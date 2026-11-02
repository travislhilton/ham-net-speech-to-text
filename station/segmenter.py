"""Frames in, transmission events out. No audio device here, so it is unit-tested anywhere.

    seg = Segmenter(gate, frame_ms=32, min_speech_ms=400, hangover_ms=900, max_tx_s=180, preroll_ms=250)
    for ev in seg.feed(frame): ...     # ("start", t0) / ("audio", np.ndarray so far) / ("end", audio, info)
"""
import collections
import time

import numpy as np


class Segmenter:
    def __init__(self, gate, sample_rate=16000, frame_ms=32, min_speech_ms=400, hangover_ms=900,
                 max_tx_s=180, preroll_ms=250, partial_every_s=1.0, clock=time.time):
        self.gate, self.sr, self.frame_ms, self.clock = gate, sample_rate, frame_ms, clock
        self.min_frames = max(1, min_speech_ms // frame_ms)
        self.hang_frames = max(1, hangover_ms // frame_ms)
        self.max_frames = int(max_tx_s * 1000 // frame_ms)
        self.partial_frames = max(1, int(partial_every_s * 1000 // frame_ms))
        self.pre = collections.deque(maxlen=max(1, preroll_ms // frame_ms))
        self.active, self.buf, self.silent, self.t0, self.n_pre = False, [], 0, None, 0
        self.level_db = None

    def feed(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-12)
        self.level_db = 20 * np.log10(rms)
        on = self.gate.is_speech(frame)
        if not self.active:
            if on:
                self.active, self.silent = True, 0
                self.n_pre = len(self.pre)
                self.buf = list(self.pre) + [frame]
                self.t0 = self.clock() - len(self.buf) * self.frame_ms / 1000
                yield ("start", self.t0)
            self.pre.append(frame)
            return
        self.buf.append(frame)
        self.silent = 0 if on else self.silent + 1
        if self.silent >= self.hang_frames or len(self.buf) >= self.max_frames:
            speech = len(self.buf) - self.silent - self.n_pre      # pre-roll is not speech
            audio = np.concatenate(self.buf)
            self.active, self.buf = False, []
            yield ("end", audio, {"t0": self.t0, "speech_frames": speech,
                                  "kerchunk": speech < self.min_frames,
                                  "duration_s": round(len(audio) / self.sr, 2)})
        elif len(self.buf) % self.partial_frames == 0:
            yield ("audio", np.concatenate(self.buf))
