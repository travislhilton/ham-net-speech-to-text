"""Set a radio's audio level just right: measure, judge against a target, and set the Windows recording level.

Two steps, both judged from the station's own meter (station/meter.py, ~10 readings a second):
  noise  FM: open the squelch for steady hiss (an unsquelched data jack always carries it). SSB/AM: a quiet
         frequency. The steady noise should sit in the target band: loud enough to hear weak stations, with
         room above it for voices.
  voice  someone talks: peaks between -12 and -3 dBFS, never at clipping (-1 dBFS; Whisper cannot undo it).
The Windows recording level (Sound -> Recording -> <input> -> Levels) is read and set through the Core Audio
endpoint volume (pycaw, Windows only). Without it, the advice says how far to turn the radio instead.

Measured on a laptop station (2026-11-05): a Digirig on a radio's speaker output, squelch open, gave hiss at
-13 dBFS with peaks at -0.7 (voices would clip); at 6 % Windows level, -42 dBFS; at 16.5 dB, -24 dBFS with peaks -11.
"""
from __future__ import annotations

import statistics
import time

NOISE_FM = (-30.0, -20.0)          # rms dBFS of open-squelch / data-jack hiss
NOISE_SSB = (-45.0, -32.0)         # rms dBFS of band noise on a quiet frequency
VOICE_PEAK = (-12.0, -3.0)         # peak dBFS while someone talks
CLIP = -1.0
DEAD = -60.0
WINDOW_S = 3.0


def band_for(mode: str | None, gate: str | None) -> tuple[float, float]:
    if gate == "carrier" or (mode or "FM").upper() in ("FM", "NFM", "DIG"):
        return NOISE_FM
    return NOISE_SSB


def stats(readings: list[tuple[float, float, float, bool]], since: float | None = None,
          voice: bool = False) -> dict:
    """readings = [(time, rms, peak, receiving)]. Noise: every reading; voice: only while receiving."""
    rs = [r for r in readings if (since is None or r[0] >= since) and (r[3] or not voice)]
    if not rs:
        return {"n": 0}
    return {"n": len(rs), "rms": round(statistics.median(r[1] for r in rs), 1),
            "peak": round(max(r[2] for r in rs), 1),
            "clip": round(sum(r[2] >= CLIP for r in rs) / len(rs), 2)}


def advise(step: str, st: dict, band: tuple[float, float]) -> dict:
    """{state: none|dead|low|ok|high|clip, change_db, say} for the noise or voice step."""
    if st.get("n", 0) < 5:
        return {"state": "none", "change_db": 0.0,
                "say": "Waiting for someone to talk…" if step == "voice" else "Measuring…"}
    rms, peak = st["rms"], st["peak"]
    if step == "voice":
        if peak >= CLIP:
            return {"state": "clip", "change_db": round(-6.0 - peak, 1),
                    "say": f"Clipping: turn it down about {round(peak + 6):.0f} dB."}
        if peak > VOICE_PEAK[1]:
            return {"state": "high", "change_db": round(-6.0 - peak, 1),
                    "say": f"Close to clipping: turn it down about {round(peak + 6):.0f} dB."}
        if peak < VOICE_PEAK[0]:
            return {"state": "low", "change_db": round(-6.0 - peak, 1),
                    "say": f"Voices are quiet: turn it up about {round(-6 - peak):.0f} dB."}
        return {"state": "ok", "change_db": 0.0, "say": "Voices are just right."}
    if rms < DEAD:
        return {"state": "dead", "change_db": 0.0,
                "say": "Almost no audio. Is the squelch open, the cable in, the radio's volume up?"}
    centre = (band[0] + band[1]) / 2
    if peak >= CLIP or rms > band[1]:
        return {"state": "clip" if peak >= CLIP else "high", "change_db": round(centre - rms, 1),
                "say": f"Too loud: turn it down about {round(rms - centre):.0f} dB."}
    if rms < band[0]:
        return {"state": "low", "change_db": round(centre - rms, 1),
                "say": f"Too quiet: turn it up about {round(centre - rms):.0f} dB."}
    return {"state": "ok", "change_db": 0.0, "say": "Just right."}


class WinGain:
    """The Windows recording level of the sound input whose name contains `name` (None elsewhere)."""

    def __init__(self, name: str):
        self.name, self.friendly = name, None
        self.vol = self._find(name)

    def _find(self, name: str):
        if not name or name.startswith("file:"):
            return None
        try:
            from pycaw.constants import DEVICE_STATE, EDataFlow
            from pycaw.pycaw import AudioUtilities
            for d in AudioUtilities.GetAllDevices(data_flow=EDataFlow.eCapture.value,
                                                  device_state=DEVICE_STATE.ACTIVE.value):
                if name.lower() in (d.FriendlyName or "").lower():
                    self.friendly = d.FriendlyName
                    return d.EndpointVolume
        except Exception:  # noqa: BLE001 — not Windows, pycaw missing, COM refused: advice only
            return None
        return None

    def get(self) -> dict | None:
        if self.vol is None:
            return None
        try:
            lo, hi, _ = self.vol.GetVolumeRange()
            return {"db": round(self.vol.GetMasterVolumeLevel(), 1), "min": round(lo, 1), "max": round(hi, 1),
                    "pct": round(self.vol.GetMasterVolumeLevelScalar() * 100), "muted": bool(self.vol.GetMute()),
                    "device": self.friendly}
        except Exception:  # noqa: BLE001
            return None

    def set_db(self, db: float) -> dict | None:
        g = self.get()
        if g is None:
            return None
        self.vol.SetMasterVolumeLevel(max(g["min"], min(g["max"], float(db))), None)
        if g["muted"]:
            self.vol.SetMute(0, None)
        return self.get()


def auto(measure, gain, band, rounds: int = 4, settle_s: float = 0.6, window_s: float = WINDOW_S,
         sleep=time.sleep, clock=time.time) -> dict:
    """Bring the noise into `band` with the Windows level: measure, set, measure again (a few rounds).
    measure(since) -> stats; gain has get()/set_db(). Returns {ok, steps, advice, gain}."""
    steps = []
    for _ in range(rounds):
        t0 = clock()
        sleep(settle_s + window_s)
        st = measure(t0 + settle_s)
        a = advise("noise", st, band)
        g = gain.get()
        steps.append({"stats": st, "advice": a, "gain": g})
        if a["state"] in ("ok", "none", "dead") or g is None:
            break
        want = max(g["min"], min(g["max"], g["db"] + a["change_db"]))
        if abs(want - g["db"]) < 0.2:                 # at the end of the Windows range: the radio must change
            a = dict(a, say=a["say"].replace("turn it", "Windows is at its " + ("maximum" if a["change_db"] > 0
                                                                                  else "minimum") + ": turn the radio's volume"))
            steps[-1]["advice"] = a
            break
        gain.set_db(want)
    return {"ok": steps[-1]["advice"]["state"] == "ok", "steps": steps, "advice": steps[-1]["advice"],
            "gain": gain.get()}
