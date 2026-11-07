"""Morse code (CW) on any radio: find a keyed tone in the audio, decode it, and say how sure we are.

Any frequency can carry CW: a repeater's CW identification, a station sending its call, an HF CW contact heard on an
SSB receiver, someone practising on a simplex channel. It runs on each radio's continuous audio (station/core.py
`_listen`), not on the gated transmissions: a keyed tone does not always open a voice gate.

Streaming (MorseWatch.feed, every audio frame): a 16 ms window every 5 ms; a frame is "tonal" when its strongest
bin between 300 and 1500 Hz stands well above the band's median (a pure tone does; hiss does not). A candidate run
starts at the first tonal frame and ends after `END_S` without one (or at `MAX_S`); then the whole run is decoded:

  1. the tone: the most common peak frequency among the tonal frames; it must hold steady (voice pitch wanders);
  2. keying: the power at that tone, frame by frame, against a threshold between its on and off levels (hysteresis);
  3. the dit length from the durations themselves (the shorter cluster of the on-times, or the shortest off-times
     when every element is the same length); 5-50 words per minute (PARIS: 1.2 s / dit);
  4. CW timing is a grid: elements 1 and 3 dits, gaps 1, 3 and 7. A run counts as Morse only when its on-times sit
     on that grid, the tone is steady and the keyed level is flat, and at least `MIN_CHARS` characters decode to
     real Morse. That keeps out voice, a courtesy beep, a steady carrier, DTMF and sub-audible tones (< 300 Hz).

Out: {"t0", "t1" (epoch s), "tone_hz", "wpm", "symbols" ("-.-. --.-" with " / " between words), "text", "chars",
"score"}. Prosigns show as <AR>, <SK>, <BT>, <KN>.
"""
from __future__ import annotations

import collections

import numpy as np

SR = 16000
WIN = 256                    # 16 ms
HOP = 80                     # 5 ms
F_LO, F_HI = 300.0, 1500.0
PEAK_DB = 12.0               # a tonal frame: its peak this far above the band median
END_S = 2.5                  # a run ends after this long with no tonal frame
MAX_S = 120.0                # ... or at this length (decoded, and a new run starts)
MIN_CHARS = 3
WPM_RANGE = (5.0, 50.0)

CODE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H", "..": "I",
    ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5", "-....": "6", "--...": "7",
    "---..": "8", "----.": "9", ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/", "-...-": "<BT>",
    ".-.-.": "<AR>", "...-.-": "<SK>", "-.--.": "<KN>", ".----.": "'", "-.-.--": "!", "---...": ":", "-....-": "-",
    ".-..-.": '"', ".--.-.": "@", "........": "<HH>",
}

_win = np.hanning(WIN).astype(np.float32)
_freqs = np.fft.rfftfreq(WIN, 1 / SR)
_band = (_freqs >= F_LO) & (_freqs <= F_HI)
_wide = (_freqs >= 200) & (_freqs <= 3000)
_band_idx = np.flatnonzero(_band)


def spectra(x: np.ndarray) -> np.ndarray:
    """Power spectra, one row per 5 ms hop."""
    x = np.asarray(x, np.float32)
    if len(x) < WIN:
        return np.zeros((0, len(_freqs)), np.float32)
    n = 1 + (len(x) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    return (np.abs(np.fft.rfft(x[idx] * _win, axis=1)) ** 2).astype(np.float32)


def tonal(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """-> (is tonal per frame, peak bin per frame)."""
    if not len(p):
        return np.zeros(0, bool), np.zeros(0, int)
    pk = _band_idx[np.argmax(p[:, _band], axis=1)]
    med = np.median(p[:, _wide], axis=1) + 1e-12
    db = 10 * np.log10(p[np.arange(len(p)), pk] / med + 1e-12)
    return db >= PEAK_DB, pk


def _runs(b: np.ndarray) -> list[tuple[bool, int, int]]:
    out, i = [], 0
    while i < len(b):
        j = i
        while j < len(b) and b[j] == b[i]:
            j += 1
        out.append((bool(b[i]), i, j))
        i = j
    return out


def _two_means(v: np.ndarray) -> tuple[float, float]:
    lv = np.log(v)
    a, b = lv.min(), lv.max()
    for _ in range(20):
        m = np.abs(lv - a) <= np.abs(lv - b)
        if m.all() or (~m).all():
            break
        a, b = lv[m].mean(), lv[~m].mean()
    return float(np.exp(a)), float(np.exp(b))


def unit_of(on: np.ndarray, off: np.ndarray) -> float | None:
    """The dit length (s) from the element and gap durations."""
    if len(on) == 0:
        return None
    lo, hi = _two_means(on)
    if hi / lo >= 2.0:                         # dits and dahs both there
        return lo if hi / lo <= 4.5 else None
    inner = off[off < 2.5 * on.max()] if len(off) else off
    if len(inner):                             # every element alike: the gaps inside letters are one dit
        g = float(np.percentile(inner, 25))
        if 0.6 <= on.mean() / g <= 1.6:
            return float(min(on.mean(), g))    # all dits
        if 2.0 <= on.mean() / g <= 4.5:
            return g                            # all dahs
    return None


def decode(x: np.ndarray, t0: float = 0.0) -> dict | None:
    """Decode a stretch of audio holding one CW run; None when it is not Morse."""
    p = spectra(x)
    if len(p) < 20:
        return None
    is_t, pk = tonal(p)
    if is_t.sum() < 10:
        return None
    k0 = int(np.bincount(pk[is_t]).argmax())
    steady = float(np.mean(np.abs(pk[is_t] - k0) <= 1))
    if steady < 0.75:
        return None
    lo = max(k0 - 1, 0)
    env = 10 * np.log10(p[:, lo:k0 + 2].sum(axis=1) + 1e-12)
    env = np.maximum(env, np.percentile(env, 92) - 40)          # digital silence would swamp the smoothing
    env = np.convolve(env, np.ones(3) / 3, "same")
    hi_l, lo_l = np.percentile(env, 92), np.percentile(env, 15)
    if hi_l - lo_l < 10:
        return None
    thr = lo_l + 0.5 * (hi_l - lo_l)
    on = np.zeros(len(env), bool)
    state = False
    for i, e in enumerate(env):                # hysteresis
        state = e > thr + 2 if not state else e > thr - 2
        on[i] = state
    runs = _runs(on)
    while runs and not runs[0][0]:
        runs.pop(0)
    while runs and not runs[-1][0]:
        runs.pop()
    runs = [r for r in runs if r[2] - r[1] >= 2 or not r[0]]      # drop single-frame blips
    if not runs:
        return None
    dur = lambda r: (r[2] - r[1]) * HOP / SR
    ons = np.array([dur(r) for r in runs if r[0]])
    offs = np.array([dur(r) for r in runs if not r[0]])
    if len(ons) < 3:
        return None
    unit = unit_of(ons, offs)
    if not unit:
        return None
    wpm = 1.2 / unit
    if not WPM_RANGE[0] <= wpm <= WPM_RANGE[1]:
        return None
    # timing on the grid: elements ~1 or ~3 dits
    grid = np.mean([min(abs(d / unit - 1), abs(d / unit - 3) / 1.5) <= 0.5 for d in ons])
    on_frames = on & is_t
    core = on & np.roll(on, 1) & np.roll(on, -1)                # the keyed level away from the element's edges
    flat = float(np.std(env[core])) if core.sum() >= 5 else 99.0
    steady_on = float(np.mean(np.abs(pk[on] - k0) <= 1)) if on.any() else 0.0
    sym, letters, words, chars, bad = [], [], [], 0, 0
    cur = ""
    for r in runs:
        d = dur(r)
        if r[0]:
            cur += "." if d < 2 * unit else "-"
            continue
        if d < 2 * unit:
            continue
        if cur:
            sym.append(cur); ch = CODE.get(cur); letters.append(ch or "?"); chars += 1; bad += ch is None
            cur = ""
        if d >= 5 * unit:
            sym.append("/"); letters.append(" ")
    if cur:
        sym.append(cur); ch = CODE.get(cur); letters.append(ch or "?"); chars += 1; bad += ch is None
    valid = (chars - bad) / chars if chars else 0.0
    score = round(float(0.35 * grid + 0.25 * valid + 0.2 * steady_on + 0.2 * min(1.0, max(0.0, (8 - flat) / 6))), 2)
    why = [n for n, bad_ in (("chars", chars < MIN_CHARS), ("grid", grid < 0.8), ("valid", valid < 0.75),
                             ("steady", steady_on < 0.85), ("flat", flat > 6.0)) if bad_]
    if why:
        decode.last_reject = (why, round(grid, 2), round(valid, 2), round(steady_on, 2), round(flat, 1), chars)
        return None
    f = float(_freqs[k0])
    if 0 < k0 < p.shape[1] - 1:                # parabolic peak for the tone's frequency
        a, b, c = np.log(p[on, k0 - 1:k0 + 2].mean(axis=0) + 1e-12)
        den = a - 2 * b + c
        if den:
            f += 0.5 * (a - c) / den * (SR / WIN)
    first = runs[0][1] * HOP / SR
    last = runs[-1][2] * HOP / SR + WIN / SR
    text = " ".join("".join(letters).split())
    return {"t0": round(t0 + first, 2), "t1": round(t0 + last, 2), "tone_hz": round(f), "wpm": round(wpm, 1),
            "symbols": " ".join(sym).replace(" / ", " / "), "text": text, "chars": chars, "score": score,
            "on_frames": int(on_frames.sum())}


class MorseWatch:
    """Per radio: feed every audio frame; returns finished CW runs (decoded) as they end."""

    def __init__(self, sr: int = SR, end_s: float = END_S, max_s: float = MAX_S):
        assert sr == SR
        self.end_s, self.max_s = end_s, max_s
        self.buf = np.zeros(0, np.float32)     # audio not yet analysed (< one hop past the last window)
        self.t = None                          # epoch time of buf[0]
        self.run: list[np.ndarray] = []        # the current candidate run's audio
        self.run_t0 = None
        self.quiet = 0.0
        self.pre = collections.deque(maxlen=int(0.3 * SR / HOP))   # a little audio before the first tonal frame

    def feed(self, frame: np.ndarray, t: float) -> list[dict]:
        """frame = mono float32 at 16 kHz; t = epoch time of its first sample."""
        out = []
        x = np.asarray(frame, np.float32).ravel()
        if self.t is None:
            self.t = t
        self.buf = np.concatenate([self.buf, x])
        n = 0 if len(self.buf) < WIN else 1 + (len(self.buf) - WIN) // HOP
        if not n:
            return out
        p = spectra(self.buf[: WIN + (n - 1) * HOP])
        is_t, _ = tonal(p)
        for i in range(n):
            hop = self.buf[i * HOP:(i + 1) * HOP]
            ht = self.t + i * HOP / SR
            if self.run_t0 is None:
                if is_t[i]:
                    self.run_t0 = ht - len(self.pre) * HOP / SR
                    self.run = list(self.pre) + [hop.copy()]
                    self.quiet = 0.0
                else:
                    self.pre.append(hop.copy())
                continue
            self.run.append(hop.copy())
            self.quiet = 0.0 if is_t[i] else self.quiet + HOP / SR
            if self.quiet >= self.end_s or len(self.run) * HOP / SR >= self.max_s:
                r = self._finish()
                if r:
                    out.append(r)
        self.buf = self.buf[n * HOP:]
        self.t += n * HOP / SR
        return out

    def _finish(self) -> dict | None:
        audio = np.concatenate(self.run) if self.run else np.zeros(0, np.float32)
        t0 = self.run_t0
        self.run, self.run_t0, self.quiet = [], None, 0.0
        self.pre.clear()
        r = decode(audio, t0)
        if r:
            a = max(0, int((r["t0"] - t0 - 0.2) * SR)); b = min(len(audio), int((r["t1"] - t0 + 0.2) * SR))
            r["audio"] = audio[a:b]
        return r

    def flush(self) -> list[dict]:
        r = self._finish() if self.run_t0 is not None else None
        return [r] if r else []


def synth(text: str, wpm: float = 20, tone: float = 700, sr: int = SR, jitter: float = 0.0, snr_db: float | None = None,
          seed: int = 0, farnsworth: float | None = None) -> np.ndarray:
    """Morse audio for tests and the bench: `text` keyed at `wpm` (optional timing jitter, noise at `snr_db`)."""
    rnd = np.random.default_rng(seed)
    inv = {v: k for k, v in CODE.items()}
    u = 1.2 / wpm
    cu = 1.2 / farnsworth if farnsworth else u
    parts: list[tuple[bool, float]] = [(False, 0.3)]
    toks = []
    i = 0
    while i < len(text):                        # <AR> style prosigns
        if text[i] == "<":
            j = text.index(">", i); toks.append(text[i:j + 1]); i = j + 1
        else:
            toks.append(text[i].upper()); i += 1
    for k, ch in enumerate(toks):
        if ch == " ":
            parts.append((False, 7 * cu - 3 * cu)); continue
        code = inv[ch]
        for m, e in enumerate(code):
            parts.append((True, (1 if e == "." else 3) * u))
            if m < len(code) - 1:
                parts.append((False, u))
        parts.append((False, 3 * cu))
    parts.append((False, 0.3))
    sig = []
    ph = 0.0
    for keyed, d in parts:
        d = max(0.004, d * (1 + jitter * rnd.standard_normal())) if jitter else d
        n = int(d * sr)
        if keyed:
            t = np.arange(n) / sr
            s = np.sin(2 * np.pi * tone * t + ph)
            ph += 2 * np.pi * tone * n / sr
            ramp = min(n // 2, int(0.004 * sr))
            if ramp:
                env = np.ones(n); env[:ramp] = np.linspace(0, 1, ramp); env[-ramp:] = np.linspace(1, 0, ramp)
                s = s * env
            sig.append(0.3 * s)
        else:
            sig.append(np.zeros(n))
    x = np.concatenate(sig).astype(np.float32)
    if snr_db is not None:
        pw = 0.3 ** 2 / 2
        x = x + rnd.standard_normal(len(x)).astype(np.float32) * np.sqrt(pw / 10 ** (snr_db / 10))
    return x.astype(np.float32)
