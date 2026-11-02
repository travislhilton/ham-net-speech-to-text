"""Live signal level, signal-to-noise ratio and a waterfall row for one radio stream, ~10 times a second.

Only the receiver's AUDIO reaches the PC, so the waterfall is the audio spectrum of what the radio hears
on its current frequency (like WSJT-X / fldigi), not an RF band scope.

Level  (dBFS of the sound input): green inside [low_dbfs, high_dbfs]; yellow just outside; red when the
       input clips (peak >= clip_dbfs) or is nearly silent (rms < dead_dbfs: unplugged / volume at zero).
SNR    while receiving: FM with the carrier gate = noise QUIETING (open-squelch hiss level minus the hiss
       left under the carrier, above the voice band); voice gate (SSB/AM) = voice-band level minus the
       noise floor (10th percentile of the last 10 s). Green >= snr_good_db, yellow >= snr_fair_db, red below.
"""
import base64
import collections

import numpy as np

DEFAULTS = {"low_dbfs": -45.0, "high_dbfs": -6.0, "clip_dbfs": -1.0, "dead_dbfs": -60.0,
            "snr_good_db": 20.0, "snr_fair_db": 10.0, "bins": 128, "frames_per_row": 3}
SPEC_FLOOR, SPEC_CEIL = -110.0, -30.0          # dBFS per bin mapped to 0..255


def level_state(rms_db: float, peak_db: float, c: dict) -> str:
    if peak_db >= c["clip_dbfs"] or rms_db < c["dead_dbfs"]:
        return "bad"
    if peak_db >= c["high_dbfs"] or rms_db < c["low_dbfs"]:
        return "warn"
    return "ok"


def snr_state(snr_db, receiving: bool, c: dict) -> str:
    if not receiving or snr_db is None:
        return "idle"
    return "ok" if snr_db >= c["snr_good_db"] else "warn" if snr_db >= c["snr_fair_db"] else "bad"


class Meter:
    def __init__(self, cfg: dict | None = None, sample_rate: int = 16000, gate_kind: str = "carrier",
                 span_hz=None):
        self.c = DEFAULTS | (cfg or {})
        self.sr, self.kind = sample_rate, gate_kind
        lo, hi = span_hz or ((0, sample_rate // 2) if gate_kind == "carrier" else (0, 4000))
        self.span = (int(lo), int(hi))
        self.acc, self.n, self.peak, self.sq, self.count = None, 0, 0.0, 0.0, 0
        self.voice_hist = collections.deque(maxlen=100)        # 10 s of 100 ms voice-band levels
        self._freqs = None

    def _bins(self, spec: np.ndarray, n: int) -> np.ndarray:
        if self._freqs is None or len(self._freqs) != len(spec):
            self._freqs = np.fft.rfftfreq(n, 1 / self.sr)
            edges = np.linspace(self.span[0], self.span[1], self.c["bins"] + 1)
            self._idx = np.clip(np.searchsorted(self._freqs, edges), 0, len(spec) - 1)
        out = np.empty(self.c["bins"])
        for i in range(self.c["bins"]):
            a, b = self._idx[i], max(self._idx[i + 1], self._idx[i] + 1)
            out[i] = spec[a:b].mean()
        return out

    def feed(self, frame: np.ndarray, receiving: bool, gate=None) -> dict | None:
        """One audio frame in; a reading out every `frames_per_row` frames (None otherwise)."""
        x = frame.astype(np.float64)
        w = np.hanning(len(x))
        spec = np.abs(np.fft.rfft(x * w)) ** 2 / (w.sum() ** 2 / 4)
        self.acc = spec if self.acc is None else self.acc + spec
        self.peak = max(self.peak, float(np.abs(x).max(initial=0.0)))
        self.sq += float((x ** 2).sum()); self.count += len(x); self.n += 1
        if self.n < self.c["frames_per_row"]:
            return None
        spec = self.acc / self.n
        rms_db = 10 * np.log10(self.sq / max(self.count, 1) + 1e-12)
        peak_db = 20 * np.log10(self.peak + 1e-12)
        f = np.fft.rfftfreq(len(frame), 1 / self.sr)
        voice_db = 10 * np.log10(spec[(f >= 300) & (f < 3000)].mean() + 1e-15)
        if self.kind == "carrier" and gate is not None and gate.floor is not None and gate.last_db is not None:
            snr = max(0.0, gate.floor - gate.last_db) if receiving else 0.0
        else:
            if not receiving:
                self.voice_hist.append(voice_db)
            noise = np.percentile(self.voice_hist, 10) if len(self.voice_hist) >= 5 else None
            snr = max(0.0, voice_db - noise) if (receiving and noise is not None) else (0.0 if noise is not None else None)
        row = np.clip((10 * np.log10(self._bins(spec, len(frame)) + 1e-15) - SPEC_FLOOR) / (SPEC_CEIL - SPEC_FLOOR), 0, 1)
        reading = {"rms": round(rms_db, 1), "peak": round(peak_db, 1),
                   "snr": None if snr is None else round(float(snr), 1), "rx": bool(receiving),
                   "ls": level_state(rms_db, peak_db, self.c), "ss": snr_state(snr, receiving, self.c),
                   "s": base64.b64encode((row * 255).astype(np.uint8).tobytes()).decode()}
        self.acc, self.n, self.peak, self.sq, self.count = None, 0, 0.0, 0.0, 0
        return reading

    def limits(self) -> dict:
        return {k: self.c[k] for k in ("low_dbfs", "high_dbfs", "clip_dbfs", "dead_dbfs", "snr_good_db", "snr_fair_db")} | \
               {"span_hz": list(self.span), "bins": self.c["bins"]}
