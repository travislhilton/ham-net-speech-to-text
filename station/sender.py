"""Sidekick's Send mode: a draft that would speak is said on the air, under the control operator's eye.

Owner 2026-11-12: "Sidekick would then reply immediately" and "do them all" (the send step, the speed work, the
dummy-load test). Part 97 does not allow automatic control of a voice station answering people (97.109(d) is for
repeaters, beacons, some data), so the control operator stays at the control point and can stop every transmission:

* A radio's Sidekick switch has a third position, **Send**, offered only on a radio whose Transmit switch is on
  (station/ptt.py; off by default).
* A draft that would speak (and passed every check) is shown with a countdown, "Sending in 3… (Esc to stop)".
  The voice clip is made during the countdown (no time lost).
* It is sent only while the operator is PRESENT: the dashboard reports keyboard or mouse use (PRESENCE_S). Away from
  the station = Draft only, and the card says so.
* At key-up, everything is checked again: still present, not stopped, nobody transmitting (the radio's carrier or
  voice gate: listen before talk), nothing new heard on that radio since the draft, the frequency known (a privileges
  check on an unknown frequency would pass by default).
* PTT through Transmitter.key() (its own watchdog: max_s), a short lead before the voice (the radio's TX ramp, a
  repeater's receiver) and a short tail after it. Esc, Stop, or any click on the dashboard drops PTT at once.
* Every send is logged to data/agent_sends.jsonl (draft, words, timings, outcome).

The voice: xAI's (the operator's chosen Grok voice, kept per words in data/tts/), or, with no key or no internet, this
PC's own Windows voice (System.Speech), so a check-in still goes out offline.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime

COUNTDOWN_S = 3.0        # the operator's window to stop a transmission before it starts
PRESENCE_S = 180.0       # keyboard / mouse on the dashboard within this long = the control operator is present
LEAD_S = 0.35            # PTT on, then the voice: the transmitter's ramp and a repeater's receiver
TAIL_S = 0.25            # after the voice, before letting go
MAX_SAY_S = 45.0         # longer than this is never sent in one transmission (the drafts are under ~30 s)


class Sender:
    def __init__(self, station, agent, play=None, voice=None, clock=time.monotonic):
        self.st, self.agent = station, agent
        self.play_fn = play or self._play
        self.voice_fn = voice or self._voice
        self.clock = clock
        self.lock = threading.Lock()
        self.out: dict[str, dict] = {}          # radio -> the transmission being prepared, counted down, or on air
        self.stops: dict[str, threading.Event] = {}
        self.last_presence = 0.0
        self.heard_at: dict[str, float] = {}   # radio -> when something was last heard on it (monotonic)
        self.log_path = station.data / "agent_sends.jsonl"

    # ----- the operator -----
    def presence(self) -> dict:
        self.last_presence = self.clock()
        return {"present": True}

    def present(self) -> bool:
        return self.clock() - self.last_presence < PRESENCE_S

    def stop(self, rid: str | None = None, why: str = "stopped by the operator") -> dict:
        """Stop one radio's transmission (or every radio's): before key-up it is cancelled, on the air PTT drops."""
        with self.lock:
            rids = [rid] if rid else list(self.out)
            for r in rids:
                ev = self.stops.get(r)
                if ev:
                    ev.set()
                o = self.out.get(r)
                if o and o["state"] in ("preparing", "counting", "on_air"):
                    o["stop_why"] = why
        return self.view()

    def heard(self, rid: str):
        """Something new was heard on this radio: a countdown on it is cancelled (someone else has the floor)."""
        self.heard_at[rid] = self.clock()

    def view(self) -> dict:
        return {"present": self.present(), "presence_s": PRESENCE_S, "countdown_s": COUNTDOWN_S,
                "out": {r: {k: v for k, v in o.items() if k != "audio"} for r, o in self.out.items()}}

    # ----- a draft that would speak -----
    def can_send(self, rid: str, ctx: dict) -> tuple[bool, str]:
        ok, why = self.st.ptt.allowed(rid)
        if not ok:
            return False, why
        if not self.present():
            return False, "the control operator is not at the station (no keyboard or mouse on the dashboard for " \
                          f"{int(PRESENCE_S // 60)} min): Draft only"
        if not ctx.get("freq_mhz"):
            return False, "the radio's frequency is not known: privileges cannot be checked"
        if ctx.get("emergency"):
            return False, "emergency or priority traffic"
        with self.lock:
            o = self.out.get(rid)
            if o and o["state"] in ("preparing", "counting", "on_air"):
                return False, "already sending on this radio"
        return True, ""

    def offer(self, rid: str, d: dict, ctx: dict) -> dict:
        """Start the countdown for a draft (it would speak, passed every check). Returns the outgoing record."""
        ok, why = self.can_send(rid, ctx)
        rec = {"id": uuid.uuid4().hex[:8], "radio": rid, "draft": d.get("id"), "kind": d.get("kind"),
               "say": d.get("say", ""), "at": datetime.now().isoformat(timespec="seconds"),
               "state": "counting" if ok else "held", "why": why, "countdown_s": COUNTDOWN_S}
        if not ok:
            self._publish(rid, rec)
            return rec
        ev = threading.Event()
        with self.lock:
            self.out[rid] = rec
            self.stops[rid] = ev
        self._publish(rid, rec)
        th = threading.Thread(target=self._run, args=(rid, rec, ev, ctx, self.clock()), daemon=True,
                              name=f"send-{rid}")
        th.start()
        self._thread = th                                  # (tests join it)
        return rec

    def _run(self, rid, rec, ev, ctx, t0):
        timing = {}
        try:
            audio = {}

            def prep():
                try:
                    audio["pcm"], audio["sr"], audio["voice"] = self.voice_fn(rec["say"])
                except Exception as e:  # noqa: BLE001
                    audio["error"] = f"{type(e).__name__}: {e}"
                timing["voice_s"] = round(self.clock() - t0, 2)
            pt = threading.Thread(target=prep, daemon=True)
            pt.start()
            end = self.clock() + COUNTDOWN_S
            while self.clock() < end:                           # the countdown: anyone keying takes the floor
                if ev.wait(0.1):
                    return self._end(rid, rec, "stopped", rec.get("stop_why") or "stopped by the operator", timing)
                if self._busy(rid) or self.heard_at.get(rid, -1e9) > t0:
                    return self._end(rid, rec, "stopped", "someone transmitted first: the floor is theirs", timing)
            pt.join(30)
            if "pcm" not in audio:
                return self._end(rid, rec, "failed", "no voice: " + audio.get("error", "the voice took too long"),
                                 timing)
            dur = len(audio["pcm"]) / float(audio["sr"])
            if dur > MAX_SAY_S:
                return self._end(rid, rec, "failed", f"{dur:.0f} s is too long for one transmission", timing)
            # listen before talk, and everything checked again at the moment of key-up
            if not self.present():
                return self._end(rid, rec, "held", "the control operator left the station", timing)
            ok, why = self.st.ptt.allowed(rid)
            if not ok:
                return self._end(rid, rec, "held", why, timing)
            if self.heard_at.get(rid, -1e9) > t0:
                return self._end(rid, rec, "stopped", "someone transmitted first: the floor is theirs", timing)
            if self._busy(rid):
                return self._end(rid, rec, "stopped", "the frequency is busy (listen before talk)", timing)
            if ev.is_set():
                return self._end(rid, rec, "stopped", rec.get("stop_why") or "stopped by the operator", timing)
            rec.update(state="on_air", voice=audio.get("voice"), seconds=round(dur, 1))
            self._publish(rid, rec)
            k0 = self.clock()
            with self.st.ptt.key(rid, seconds=dur + LEAD_S + TAIL_S + 2):
                if not ev.wait(LEAD_S):
                    timing["key_to_voice_s"] = round(self.clock() - k0, 2)
                    self.play_fn(rid, audio["pcm"], audio["sr"], ev)
                    if not ev.is_set():
                        ev.wait(TAIL_S)
            timing["on_air_s"] = round(self.clock() - k0, 2)
            if ev.is_set():
                return self._end(rid, rec, "stopped", rec.get("stop_why") or "stopped on the air: PTT dropped", timing)
            self.agent.sent(rid, rec)                          # the ID clock and the contact move on real sends only
            return self._end(rid, rec, "sent", "", timing)
        except Exception as e:  # noqa: BLE001 - a send never stops the station
            return self._end(rid, rec, "failed", f"{type(e).__name__}: {e}", timing)

    def _busy(self, rid) -> bool:
        r = self.st.radios.get(rid) or {}
        if r.get("carrier"):
            return True
        try:
            recent = list(self.st.level_recent.get(rid) or [])[-3:]   # (time, rms, peak, rx): the voice gate on HF
            return any(x[3] for x in recent if time.time() - x[0] < 1.0)
        except Exception:  # noqa: BLE001
            return False

    def _end(self, rid, rec, state, why, timing):
        rec.update(state=state, why=why, timing=timing, done=datetime.now().isoformat(timespec="seconds"))
        self._publish(rid, rec)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in rec.items()}) + "\n")
        except OSError:
            pass
        return rec

    def _publish(self, rid, rec):
        try:
            self.st.bus.publish("agent", {"send": {"radio": rid, **rec}})
        except Exception:  # noqa: BLE001
            pass

    # ----- the voice and the sound card -----
    def _voice(self, text: str):
        """PCM for the words: the chosen Grok voice (kept per words), else this PC's own Windows voice."""
        import numpy as np
        import soundfile as sf
        try:
            mp3 = self.st.tts.clip(text)
            pcm, sr = sf.read(io.BytesIO(mp3), dtype="float32")
            voice = "xai"
        except (LookupError, ValueError) as e:
            pcm, sr = windows_voice(text)
            voice = f"this PC's voice ({e})"
        if pcm.ndim > 1:
            pcm = pcm.mean(axis=1)
        return np.ascontiguousarray(pcm, dtype="float32"), int(sr), voice

    def _play(self, rid, pcm, sr, ev):
        """Play to the radio's transmit audio output (Radios… → Transmit → audio out); stops at once on ev."""
        import numpy as np
        import sounddevice as sd
        from .radios import pick_index
        t = self.st.ptt.cfg(rid) or {}
        devs = list(sd.query_devices())
        outs = [dict(d, name=d["name"]) if d.get("max_output_channels", 0) > 0 else dict(d, name="")
                for d in devs]
        idx = pick_index(t.get("out") or "", outs)
        if idx is None:
            raise RuntimeError(f"the transmit audio output {t.get('out')!r} is not connected")
        rate = int(devs[idx].get("default_samplerate") or 48000)
        if rate != sr:                                          # linear resample: speech, a few kHz wide
            n = int(round(len(pcm) * rate / sr))
            pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype("float32")
        level = float(t.get("level") or 0.5)                    # Transmit test… sets it (deviation)
        sd.play(np.clip(pcm * level, -1, 1), rate, device=idx, blocking=False)
        end = time.monotonic() + len(pcm) / rate + 0.2
        while time.monotonic() < end:
            if ev.wait(0.05):
                sd.stop()
                return
        sd.wait()


def windows_voice(text: str):
    """Windows' own speech (System.Speech): offline, no key. A WAV made by PowerShell, read back."""
    import soundfile as sf
    if os.name != "nt":
        raise LookupError("no voice: no xAI clip and no Windows voice on this computer")
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$s.Rate = 0; $s.SetOutputToWaveFile($env:HN_WAV); $s.Speak($env:HN_SAY); $s.Dispose()")
        env = dict(os.environ, HN_WAV=path, HN_SAY=text)
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                       capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pcm, sr = sf.read(path, dtype="float32")
        return pcm, sr
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
