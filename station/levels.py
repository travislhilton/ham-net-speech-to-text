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

import numpy as np

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


# ---- the radio's volume knob: the cleanest setting --------------------------------------------------------
# A handheld's audio amplifier adds its own hiss and hum as its volume goes up, and distorts near the top. At
# each knob position: squelch OPEN (hiss = the receiver's own noise, the reference) and squelch CLOSED (what is
# left = the amplifier, hum, the cable and the sound input). Margin = open minus closed; bigger is cleaner.
# Distortion: the crest factor (peak over rms per 100 ms) of open-squelch hiss is ~10.8 dB for clean noise and
# falls toward ~3.6 dB when the amplifier clips; a live handheld at a good setting measured 10.0 (2026-11-05).
CREST_MIN = 8.0
KNOBS = ["1/8", "1/4", "3/8", "1/2", "5/8", "3/4", "7/8"]   # of the knob's full turn, low to high
MARGIN_SLACK = 1.5                 # within this of the best margin, the LOWER volume wins (headroom for voices)


def analyze(x: np.ndarray, sr: int = 16000) -> dict:
    x = np.asarray(x, dtype=np.float64)
    n = int(sr / 10)
    b = x[: len(x) // n * n].reshape(-1, n) if len(x) >= n else x.reshape(1, -1)
    rms = np.sqrt((b ** 2).mean(1)) + 1e-12
    pk = np.abs(b).max(1) + 1e-12
    f = np.fft.rfftfreq(len(x), 1 / sr)
    p = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    hum = sum(p[(f > h - 3) & (f < h + 3)].sum() for h in (50, 60, 100, 120, 150, 180))
    tot = p[(f > 40) & (f < 4000)].sum() + 1e-30
    return {"rms": round(float(20 * np.log10(np.median(rms))), 1), "peak": round(float(20 * np.log10(pk.max())), 1),
            "crest": round(float(np.median(20 * np.log10(pk / rms))), 1),
            "hum": round(float(10 * np.log10(hum / tot + 1e-12)), 1)}


def knob_point(p: dict) -> dict:
    """One knob position: {open, closed} analyses → margin, distortion, a word for it."""
    o, c = p.get("open"), p.get("closed")
    out = dict(p)
    if o:
        out["distorted"] = o["peak"] >= CLIP or o["crest"] < CREST_MIN
    if o and c:                    # the closed reading at the open one's Windows level (dB for dB)
        shift = (p.get("open_gain_db") or 0.0) - (p.get("closed_gain_db") or 0.0) \
            if p.get("open_gain_db") is not None and p.get("closed_gain_db") is not None else 0.0
        out["margin"] = round(o["rms"] - (c["rms"] + shift), 1)
    return out


def best_knob(points: list[dict]) -> dict:
    """The lowest knob position whose margin is within MARGIN_SLACK of the best undistorted one."""
    done = [knob_point(p) for p in points]
    good = [p for p in done if "margin" in p and not p.get("distorted")]
    if not good:
        bad = [p for p in done if p.get("distorted")]
        return {"best": None, "points": done, "say": "Every position measured distorts: try lower volume settings." if bad
                else "Measure at least one position with the squelch open and closed."}
    top = max(p["margin"] for p in good)
    pick = min((p for p in good if p["margin"] >= top - MARGIN_SLACK), key=lambda p: KNOBS.index(p["knob"]))
    return {"best": pick["knob"], "margin": pick["margin"], "points": done,
            "say": f"Cleanest: “{pick['knob']}” ({pick['margin']:.0f} dB between hiss and the floor). Set the knob "
                   "there, open the squelch and press Adjust automatically."}


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
            from .radios import best_endpoint
            devs = {d.FriendlyName or "": d for d in AudioUtilities.GetAllDevices(
                data_flow=EDataFlow.eCapture.value, device_state=DEVICE_STATE.ACTIVE.value)}
            f = best_endpoint(name, list(devs))          # exact name first: "Digirig Lite" ≠ "Digirig Lite NET-REC"
            if f is not None:
                self.friendly = f
                return devs[f].EndpointVolume
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
