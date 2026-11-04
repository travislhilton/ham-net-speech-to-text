"""Frames in, transmission events out. No audio device here, so it is unit-tested anywhere.

    seg = Segmenter(gate, frame_ms=32, min_speech_ms=400, hangover_ms=900, max_tx_s=180, preroll_ms=500)
    for ev in seg.feed(frame): ...     # ("start", t0) / ("audio", np.ndarray so far) / ("end", audio, info)

How a transmission is cut (measured on a 24-minute FM repeater net, 2026-11-03; docs in README "Segmenting"):
- the gate opens -> "start" (with `preroll_ms` of what came before, so a first syllable the gate heard late
  survives); it closes after `hangover_ms` of no carrier/voice -> "end".
- a short piece (under `merge_max_s` of speech) is held `merge_gap_ms` after the hangover: if the gate opens
  again in that time it is the same transmission (a VAD gap inside a call sign on HF, a carrier drop-out).
  A kerchunk held this way is rescued by the speech that follows it.
- a carrier that stays up (repeater hang time) is split where the voice pauses (`split_pause_ms`) or the
  carrier dips. The cut is made when the voice comes back, in the MIDDLE of the pause, so neither piece starts
  or ends on a word. A piece is only cut off once the next one has `min_speech_ms` of voice: a fragment
  ("Over.", the tail of a call sign) stays with its neighbour instead of becoming its own transmission or
  being dropped, and a repeater tail with no voice after the last pause is trimmed, not transcribed alone.
"""
import collections
import time

import numpy as np


class VoicePauses:
    """Finds pauses in speech by the loudness of the voice band (300-3000 Hz), relative to what this radio has
    sounded like over the last ~30 s WHILE THE GATE WAS OPEN. Needed when the carrier never drops between
    speakers (repeater hang time, a net control that keeps the carrier up): a 3-minute block of many speakers
    makes Whisper loop. Measured on the 2026-11-02 145.900 net: one 180 s block looped ("KG-7, KG-7, ...");
    split at its 18 pauses >= 0.8 s into 17 pieces it read cleanly.

    Only gate-open frames teach the levels (2026-11-03): an FM data jack's open-squelch hiss is LOUDER in the
    voice band than a weak station's speech, so with the hiss in the history the "loud" reference was the hiss
    and a weak station's whole check-in read as a pause (cut mid-call, then dropped as a kerchunk)."""
    def __init__(self, sample_rate=16000, frame_len=512, history_s=30.0, frame_ms=32, ratio=0.35):
        f = np.fft.rfftfreq(frame_len, 1 / sample_rate)
        self.band = (f >= 300) & (f <= 3000)
        self.win = np.hanning(frame_len)
        self.hist = collections.deque(maxlen=int(history_s * 1000 / frame_ms))
        self.min_hist = int(5000 / frame_ms)
        self.ratio = ratio

    def ready(self) -> bool:
        return len(self.hist) >= self.min_hist

    def quiet(self, frame, gate_open=True) -> bool | None:
        """True = a pause, False = voice, None = cannot judge yet. A frame with the gate closed (a carrier dip
        inside a transmission) is a pause and is not learned from."""
        x = frame.astype(np.float64)
        if len(x) != len(self.win):
            return None
        if not gate_open:
            return True if self.ready() else None
        db = 10 * np.log10(np.sum(np.abs(np.fft.rfft(x * self.win))[self.band] ** 2) + 1e-12)
        self.hist.append(db)
        if not self.ready():
            return None
        lo, hi = np.percentile(self.hist, 10), np.percentile(self.hist, 90)
        if hi - lo < 4:                                   # no speech-vs-pause contrast to judge by
            return None
        return bool(db < lo + self.ratio * (hi - lo))


class Segmenter:
    def __init__(self, gate, sample_rate=16000, frame_ms=32, min_speech_ms=400, hangover_ms=900,
                 max_tx_s=180, preroll_ms=500, partial_every_s=1.0, clock=time.time,
                 split_pause_ms=0, min_split_s=2.0, merge_gap_ms=0, merge_max_s=3.0, dip_split_ms=96,
                 pause_ratio=0.35):
        self.gate, self.sr, self.frame_ms, self.clock = gate, sample_rate, frame_ms, clock
        fr = lambda ms: max(1, int(ms) // frame_ms)
        self.min_frames = fr(min_speech_ms)
        self.hang_frames = fr(hangover_ms)
        self.max_frames = int(max_tx_s * 1000 // frame_ms)
        self.partial_frames = fr(partial_every_s * 1000)
        self.pre = collections.deque(maxlen=fr(preroll_ms))
        self.merge_frames = int(merge_gap_ms) // frame_ms          # 0 = off
        self.merge_max_frames = int(merge_max_s * 1000 // frame_ms)
        self.active, self.buf, self.silent, self.t0, self.n_pre = False, [], 0, None, 0
        self.level_db = None
        # split a long open gate at speech pauses (0 = off): see VoicePauses
        self.pauses = (VoicePauses(sample_rate, int(sample_rate * frame_ms / 1000), frame_ms=frame_ms, ratio=pause_ratio)
                       if split_pause_ms else None)
        self.pause_frames = fr(split_pause_ms) if split_pause_ms else 0
        self.dip_frames = fr(dip_split_ms) if dip_split_ms else 0
        self.min_split_frames = int(min_split_s * 1000 // frame_ms)
        self._reset_piece()

    def _reset_piece(self):
        self.on_frames = 0            # frames with the gate open (after the pre-roll)
        self.oflags = []              # per frame of buf: the gate was open
        self.bridged = 0              # times the gate re-opened inside the merge wait (same transmission)
        self.vflags = []              # per frame of buf: True voice / False pause / None unknown
        self.quiet_run, self.quiet_start, self.dip_in_run, self.dip_end = 0, None, False, None
        self.cut = None               # a pending split: index into buf, confirmed once the rest has voice

    def _voiced(self, a=0, b=None) -> int:
        """Frames with voice in buf[a:b]; a gate-open frame the voice tracker could not judge counts as voice."""
        return sum(1 for v, o in zip(self.vflags[a:b], self.oflags[a:b]) if v or (v is None and o))

    def _speech(self) -> int:
        """Frames of speech in the piece: the gate's open frames, or (when the voice is being tracked) the
        frames with voice in them — a repeater's carrier with nobody talking is not speech."""
        return self._voiced() if self.pauses else self.on_frames

    def _info(self, audio, **kw):
        speech = kw.pop("speech", None)
        speech = self._speech() if speech is None else speech
        return {"t0": self.t0, "speech_frames": speech, "kerchunk": speech < self.min_frames,
                "duration_s": round(len(audio) / self.sr, 2), **kw}

    def _split_at(self, c, kerchunk=False):
        """End the piece at buf[:c] and start the next one with buf[c:]."""
        head, tail, tv, to = self.buf[:c], self.buf[c:], self.vflags[c:], self.oflags[c:]
        audio = np.concatenate(head)
        speech = 0 if kerchunk else self._voiced(0, c)
        yield ("end", audio, self._info(audio, speech=speech, split=True))
        t_next = self.t0 + c * self.frame_ms / 1000
        self.buf, self.n_pre, self.t0 = tail, 0, t_next
        self._reset_piece()
        self.vflags, self.oflags, self.on_frames = tv, to, sum(to)
        yield ("start", self.t0)

    def feed(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-12)
        self.level_db = 20 * np.log10(rms)
        on = self.gate.is_speech(frame)
        q = self.pauses.quiet(frame, on) if self.pauses else None
        if not self.active:
            if on:
                self.active, self.silent = True, 0
                self.n_pre = len(self.pre)
                self.buf = list(self.pre) + [frame]
                self._reset_piece()
                self.vflags = [None] * self.n_pre + [None if q is None else not q]
                self.oflags = [False] * self.n_pre + [True]
                self.on_frames = 1
                self.t0 = self.clock() - len(self.buf) * self.frame_ms / 1000
                yield ("start", self.t0)
            self.pre.append(frame)
            return
        self.buf.append(frame)
        self.vflags.append(None if q is None else not q)
        self.oflags.append(bool(on))
        if on and self.silent > self.hang_frames:
            self.bridged += 1                                     # back inside the merge wait: same transmission
        self.silent = 0 if on else self.silent + 1
        self.on_frames += 1 if on else 0
        if self.pauses:
            yield from self._pauses(q, on)
        limit = self.hang_frames
        if self.merge_frames and self._speech() < self.merge_max_frames:
            limit += self.merge_frames                            # a short piece waits for its continuation
        if self.silent >= limit or len(self.buf) >= self.max_frames:
            yield from self._end()
        elif len(self.buf) % self.partial_frames == 0:
            yield ("audio", np.concatenate(self.buf))

    def _pauses(self, q, on):
        """Pause/dip bookkeeping inside an open transmission; yields a split when one is confirmed."""
        if q:                                                     # a pause (or a carrier dip) goes on
            if self.quiet_run == 0:
                self.quiet_start = len(self.buf) - 1
            self.quiet_run += 1
            if not on:
                self.dip_in_run, self.dip_end = True, len(self.buf)  # the carrier is back after this frame
        elif q is False:                                          # voice: does it end a pause long enough to cut?
            run, start, dip = self.quiet_run, self.quiet_start, self.dip_in_run
            self.quiet_run, self.dip_in_run = 0, False
            if run and (run >= self.pause_frames or (dip and self.dip_frames and run >= self.dip_frames)):
                # the middle of a pause; after a short dip, where the carrier came back: the dip is the last
                # speaker's squelch tail and the next one often talks the moment they key up
                c = start + run // 2 if run >= self.pause_frames else self.dip_end
                if c >= self.min_split_frames:
                    yield from self._boundary(c)
            if self.cut is not None and self._voiced(self.cut) >= self.min_frames:
                yield from self._split_at(self.cut)               # the next piece has speech: cut confirmed

    def _boundary(self, c):
        if self.cut is not None:
            # the piece after the last pause never reached min_speech_ms of voice: it stays with the piece
            # before it (the end of a call sign, "Over."), and the cut moves to this pause
            self.cut = c
            return
        before = self._voiced(0, c)
        if before == 0:
            yield from self._split_at(c, kerchunk=True)           # dead carrier before the speech: drop it
        elif before >= self.min_frames:
            self.cut = c                                          # cut here once the rest proves to be speech
        # else: too little voice to stand alone — it joins the speech that follows

    def _end(self, **kw):
        trim = self.silent - self.hang_frames if self.silent > self.hang_frames else 0
        if self.cut is not None and self._voiced(self.cut) == 0:
            keep = self.cut                                       # a voiceless repeater tail after the last pause
        else:
            keep = len(self.buf) - trim                           # less the merge wait that found nothing
        self.buf, self.vflags, self.oflags = self.buf[:keep], self.vflags[:keep], self.oflags[:keep]
        self.on_frames = sum(self.oflags)
        audio = np.concatenate(self.buf)
        self.active, self.buf = False, []
        info = self._info(audio, **kw)                            # pre-roll and hangover are not speech
        if self.cut is not None:
            info["merged"] = True                                 # a fragment kept with its neighbour
        if self.bridged:
            info["bridged"] = self.bridged                        # gaps bridged by the merge wait
        self._reset_piece()
        yield ("end", audio, info)

    def flush(self):
        """End a transmission in progress now (the operator paused the radio)."""
        if not self.active:
            return
        yield from self._end(paused=True)
