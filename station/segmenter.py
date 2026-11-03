"""Frames in, transmission events out. No audio device here, so it is unit-tested anywhere.

    seg = Segmenter(gate, frame_ms=32, min_speech_ms=400, hangover_ms=900, max_tx_s=180, preroll_ms=250)
    for ev in seg.feed(frame): ...     # ("start", t0) / ("audio", np.ndarray so far) / ("end", audio, info)
"""
import collections
import time

import numpy as np


class VoicePauses:
    """Finds pauses in speech by the loudness of the voice band (300-3000 Hz), relative to what this radio has
    sounded like over the last ~30 s. Needed when the carrier never drops between speakers (repeater hang
    time, a net control that keeps the carrier up): a 3-minute block of many speakers makes Whisper loop.
    Measured on the 2026-11-02 145.900 net: one 180 s block looped ("KG-7, KG-7, ..."); split at its 18
    pauses >= 0.8 s into 17 pieces it read cleanly."""
    def __init__(self, sample_rate=16000, frame_len=512, history_s=30.0, frame_ms=32):
        f = np.fft.rfftfreq(frame_len, 1 / sample_rate)
        self.band = (f >= 300) & (f <= 3000)
        self.win = np.hanning(frame_len)
        self.hist = collections.deque(maxlen=int(history_s * 1000 / frame_ms))
        self.min_hist = int(5000 / frame_ms)

    def quiet(self, frame) -> bool | None:
        x = frame.astype(np.float64)
        if len(x) != len(self.win):
            return None
        db = 10 * np.log10(np.sum(np.abs(np.fft.rfft(x * self.win))[self.band] ** 2) + 1e-12)
        self.hist.append(db)
        if len(self.hist) < self.min_hist:
            return None
        lo, hi = np.percentile(self.hist, 10), np.percentile(self.hist, 90)
        if hi - lo < 4:                                   # no speech-vs-pause contrast to judge by
            return None
        return db < lo + 0.35 * (hi - lo)


class Segmenter:
    def __init__(self, gate, sample_rate=16000, frame_ms=32, min_speech_ms=400, hangover_ms=900,
                 max_tx_s=180, preroll_ms=250, partial_every_s=1.0, clock=time.time,
                 split_pause_ms=0, min_split_s=2.0):
        self.gate, self.sr, self.frame_ms, self.clock = gate, sample_rate, frame_ms, clock
        self.min_frames = max(1, min_speech_ms // frame_ms)
        self.hang_frames = max(1, hangover_ms // frame_ms)
        self.max_frames = int(max_tx_s * 1000 // frame_ms)
        self.partial_frames = max(1, int(partial_every_s * 1000 // frame_ms))
        self.pre = collections.deque(maxlen=max(1, preroll_ms // frame_ms))
        self.active, self.buf, self.silent, self.t0, self.n_pre = False, [], 0, None, 0
        self.level_db = None
        # split a long open gate at speech pauses (0 = off): see VoicePauses
        self.pauses = VoicePauses(sample_rate, int(sample_rate * frame_ms / 1000), frame_ms=frame_ms) if split_pause_ms else None
        self.pause_frames = max(1, split_pause_ms // frame_ms) if split_pause_ms else 0
        self.min_split_frames = int(min_split_s * 1000 // frame_ms)
        self.quiet_run, self.voiced = 0, 0

    def feed(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-12)
        self.level_db = 20 * np.log10(rms)
        on = self.gate.is_speech(frame)
        q = self.pauses.quiet(frame) if self.pauses else None
        if not self.active:
            if on:
                self.active, self.silent, self.quiet_run, self.voiced = True, 0, 0, 0
                self.n_pre = len(self.pre)
                self.buf = list(self.pre) + [frame]
                self.t0 = self.clock() - len(self.buf) * self.frame_ms / 1000
                yield ("start", self.t0)
            self.pre.append(frame)
            return
        self.buf.append(frame)
        self.silent = 0 if on else self.silent + 1
        if q is not None:
            self.quiet_run = self.quiet_run + 1 if q else 0
            self.voiced += 0 if q else 1
        if (on and self.pause_frames and self.quiet_run >= self.pause_frames
                and len(self.buf) - self.n_pre >= self.min_split_frames):
            audio = np.concatenate(self.buf)                    # the gate is still open: cut at the pause
            yield ("end", audio, {"t0": self.t0, "speech_frames": self.voiced, "split": True,
                                  "kerchunk": self.voiced < self.min_frames,
                                  "duration_s": round(len(audio) / self.sr, 2)})
            self.buf, self.n_pre, self.quiet_run, self.voiced = [], 0, 0, 0
            self.t0 = self.clock()
            yield ("start", self.t0)
            return
        if self.silent >= self.hang_frames or len(self.buf) >= self.max_frames:
            speech = len(self.buf) - self.silent - self.n_pre      # pre-roll is not speech
            if self.pauses and self.voiced and self.pause_frames:
                speech = min(speech, self.voiced)              # a split piece: count only frames with voice
            audio = np.concatenate(self.buf)
            self.active, self.buf = False, []
            yield ("end", audio, {"t0": self.t0, "speech_frames": speech,
                                  "kerchunk": speech < self.min_frames,
                                  "duration_s": round(len(audio) / self.sr, 2)})
        elif len(self.buf) % self.partial_frames == 0:
            yield ("audio", np.concatenate(self.buf))

    def flush(self):
        """End a transmission in progress now (the operator paused the radio)."""
        if not self.active:
            return
        audio = np.concatenate(self.buf)
        speech = len(self.buf) - self.silent - self.n_pre
        if self.pauses and self.pause_frames:
            speech = min(speech, self.voiced)
        self.active, self.buf = False, []
        yield ("end", audio, {"t0": self.t0, "speech_frames": speech, "kerchunk": speech < self.min_frames,
                              "duration_s": round(len(audio) / self.sr, 2), "paused": True})
