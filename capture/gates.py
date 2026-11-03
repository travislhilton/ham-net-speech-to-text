"""Transmission gates that need only numpy (unit-tested on the Mac; recorder.py wires them in).

CarrierGate is a software noise squelch for a radio DATA jack, which outputs UNSQUELCHED
discriminator audio: with no signal it is loud flat hiss; when a carrier arrives the hiss above
the voice band collapses. Measured on W9XYZ-5 (FTM-150R -> DRA-50M), 2026-11-02: the 4.5-7.5 kHz
band dropped 35-48 dB under a carrier. The gate tracks the open-squelch hiss level and opens when
the band falls `open_db` below it; it closes when the band comes back within `close_db`."""
import numpy as np


class CarrierGate:
    def __init__(self, cfg: dict, sample_rate: int = 16000):
        self.sr = sample_rate
        self.lo, self.hi = cfg.get("band_hz", [4500, 7500])
        self.open_db = float(cfg.get("open_db", 15))
        self.close_db = float(cfg.get("close_db", 8))
        self.alpha = float(cfg.get("floor_alpha", 0.02))   # floor tracking per idle frame
        self.floor = None                                   # open-squelch hiss level, dB
        self.carrier = False
        self.last_db = None

    def band_db(self, frame: np.ndarray) -> float:
        n = len(frame)
        spec = np.abs(np.fft.rfft(frame * np.hanning(n))) ** 2
        f = np.fft.rfftfreq(n, 1 / self.sr)
        band = spec[(f >= self.lo) & (f < self.hi)]
        return float(10 * np.log10(band.mean() + 1e-12))

    def is_speech(self, frame: np.ndarray) -> bool:
        db = self.last_db = self.band_db(frame)
        if self.floor is None:
            self.floor = db
        if self.carrier:
            self.carrier = db < self.floor - self.close_db
        else:
            self.carrier = db < self.floor - self.open_db
            if not self.carrier:
                # follow the hiss level when idle; rise fast (a louder floor is never a carrier),
                # fall slowly (a dip is more likely the start of a carrier than a quieter receiver)
                a = 0.3 if db > self.floor else self.alpha
                self.floor += a * (db - self.floor)
        return self.carrier


class SileroGate:
    """Voice-activity gate for radios with no carrier to detect (SSB/HF, or already-squelched audio).
    Silero VAD on 512-sample (32 ms) frames at 16 kHz; torch is imported only when this gate is used."""
    def __init__(self, cfg: dict):
        import torch
        from silero_vad import load_silero_vad
        torch.set_num_threads(2)
        self.torch, self.model = torch, load_silero_vad()
        self.th = float(cfg.get("threshold", 0.5))
        self.last_prob = 0.0

    def is_speech(self, frame16) -> bool:
        with self.torch.no_grad():
            self.last_prob = self.model(self.torch.from_numpy(frame16.astype("float32")), 16000).item()
        return self.last_prob >= self.th
