"""The accurate pass with context: a net's transmissions are put back on one timeline, decoded in windows that cut
only at real breaks, twice at shifted window phases, and the overlapping hypotheses are merged word by word and
mapped back to each transmission by time.

Why (measured 2026-11-02/03, see README "Accurate pass"): the station cuts a repeater net at 0.8 s voice pauses, so
a transmission is often half a turn or one speaker's tail plus the next speaker's head, 1-3 s long. Whisper given a
1-3 s clip with no context reads "Thank you." into a courtesy beep and garbles calls it would read in a sentence;
given a prompt it echoes the prompt. Given the clip inside the 20-30 s around it, it hears the whole turn.

Pure numpy here; the recogniser is passed in (`decode(audio, prompt) -> segments` in mlx_whisper's shape:
[{"start", "end", "avg_logprob", "compression_ratio", "no_speech_prob", "words": [{"word", "start", "end",
"probability"}]}]), so all of it is unit-tested without a GPU.

    clips = [Clip(id, start_s, audio), ...]           # start_s: seconds on any common clock (the log's start)
    out = transcribe_clips(clips, decode, targets={ids})   # -> {id: {"text", "words", "conf", "flags"}}
"""
from __future__ import annotations

import difflib
import re
import zlib
from dataclasses import dataclass, field

import numpy as np

SR = 16000
JOIN_TOL_S = 1.05        # the log's start is truncated to whole seconds: a gap under this is a pause-split piece
SEP_S = 0.5              # silence put between transmissions that were apart on the air
WIN_MAX_S = 29.0         # Whisper hears 30 s; leave room so a window never runs into its own padding edge
WIN_MIN_S = 12.0
WIN_LONG_MIN_S = 22.0    # windows of 22-29 s beat 12-28 s (WER 52.8 vs 55.3 %): more context per window
JOIN_SHIFT_S = 0.4       # see owner_bounds
LOW_CONF = 0.3           # mean word probability below this: the station keeps the live text (synthetic: 56.2 vs 56.5 %
                         # WER; on real nets the < 0.3 texts were mostly words from a neighbouring turn)
EDGE_S = 1.5           # words this close to a window cut (not a real start/end) are distrusted
NOISE_PHRASES = {"thank you", "thanks", "so", "okay", "ok", "i don't know", "bye", "you", "thank you very much",
                 "thanks for watching", "thank you for watching", "oh", "um", "uh", "hmm", "yeah", "the", "i",
                 "thank you so much", "bye bye", "all right", "alright"}
SPELLED = set("alpha alfa bravo charlie delta echo foxtrot golf hotel india juliet juliett kilo lima mike november oscar "
              "papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu zero one two three four five six "
              "seven eight nine niner".split())


@dataclass
class Clip:
    id: str
    start: float                 # seconds (any epoch, the same for all clips)
    audio: np.ndarray            # float32 mono 16 kHz
    target: bool = True          # False = context only (its text is not wanted back, but it is decoded)

    @property
    def dur(self) -> float:
        return len(self.audio) / SR


@dataclass
class Layout:
    audio: np.ndarray
    spans: dict = field(default_factory=dict)       # id -> (a_s, b_s) on the stitched timeline
    breaks: list = field(default_factory=list)       # (t_s, strength): 2 = transmissions apart on air, 1 = a split
    order: list = field(default_factory=list)


def stitch(clips: list[Clip], join_tol=JOIN_TOL_S, sep_s=SEP_S) -> Layout:
    """Clips in time order on one timeline. Pieces that followed each other on the air (a pause split: the next
    piece starts where the last ended, within the log's 1 s resolution) are joined directly, so a turn that was cut
    is whole again; transmissions that were apart get `sep_s` of silence between them (the real gap is not kept:
    Whisper needs to know there was a break, not how long it was)."""
    clips = sorted(clips, key=lambda c: c.start)
    parts, spans, breaks, t = [], {}, [], 0.0
    prev_end = None
    for c in clips:
        x = np.asarray(c.audio, np.float32)
        if prev_end is not None:
            gap = c.start - prev_end
            if gap > join_tol:
                parts.append(np.zeros(int(sep_s * SR), np.float32))
                breaks.append((t + sep_s / 2, 2))
                t += int(sep_s * SR) / SR
            else:
                breaks.append((t, 1))
        spans[c.id] = (t, t + len(x) / SR)
        parts.append(x)
        t += len(x) / SR
        prev_end = c.start + c.dur
    audio = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return Layout(audio, spans, breaks, [c.id for c in clips])


def voice_db(x: np.ndarray, frame=512) -> np.ndarray:
    """Voice-band (300-3000 Hz) level per 32 ms frame, dB."""
    n = len(x) // frame
    if n == 0:
        return np.zeros(0)
    X = x[: n * frame].reshape(n, frame).astype(np.float64)
    f = np.fft.rfftfreq(frame, 1 / SR)
    band = (f >= 300) & (f <= 3000)
    return 10 * np.log10((np.abs(np.fft.rfft(X * np.hanning(frame), axis=1)) ** 2)[:, band].sum(1) + 1e-12)


def quiet_points(x: np.ndarray, frame=512, min_run=6) -> list[tuple[float, float]]:
    """(time_s, depth) at the middle of each pause (a run of >= min_run quiet frames) inside long audio; depth =
    run length in seconds. Where a window must cut inside one long transmission it cuts at the longest pause."""
    db = voice_db(x, frame)
    if len(db) < 10:
        return []
    lo, hi = np.percentile(db, 3), np.percentile(db, 90)
    if hi - lo < 4:
        return []
    q = db < lo + 0.35 * (hi - lo)
    out, i = [], 0
    while i < len(q):
        if q[i]:
            j = i
            while j < len(q) and q[j]:
                j += 1
            if j - i >= min_run:
                out.append(((i + j) / 2 * frame / SR, (j - i) * frame / SR))
            i = j
        else:
            i += 1
    return out


def cut_points(lay: Layout) -> list[tuple[float, float]]:
    """Every place a window may begin or end, with a score: real breaks between transmissions first, then the
    joins of pause-split pieces, then pauses inside long pieces (by length)."""
    pts = [(t, 10.0 if s == 2 else 5.0) for t, s in lay.breaks]
    for cid, (a, b) in lay.spans.items():
        if b - a > WIN_MIN_S:
            x = lay.audio[int(a * SR):int(b * SR)]
            pts += [(a + t, min(4.0, 1.0 + d)) for t, d in quiet_points(x)]
    return sorted(pts)


def plan(lay: Layout, max_s=WIN_MAX_S, min_s=WIN_MIN_S, phase=0.0) -> list[tuple[float, float]]:
    """Windows tiling the timeline, each <= max_s, cut at the best-scoring point in [start+min_s, start+max_s]
    (latest of the best, so windows are long); no cut point there -> cut at max_s (rare: a 28 s turn with no pause).
    `phase` > 0 makes the first window end early (at about phase), giving a second tiling whose cuts fall in the
    middle of the first one's windows."""
    total = len(lay.audio) / SR
    pts = cut_points(lay)
    wins, a = [], 0.0
    first = True
    while a < total - 0.05:
        if total - a <= max_s and not (first and phase):
            wins.append((a, total)); break
        lo, hi = (a + max(phase - 4, 2.0), a + phase + 4) if (first and phase) else (a + min_s, a + max_s)
        hi = min(hi, total)
        cand = [(s, t) for t, s in pts if lo <= t <= hi]
        if cand:
            # best score; among equals the later one
            b = max(cand, key=lambda st: (st[0], st[1]))[1]
        else:
            b = min(a + (phase if first and phase else max_s), total)
        wins.append((a, b))
        a, first = b, False
    return wins


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", w.lower())


def compression_ratio(text: str) -> float:
    b = text.encode()
    return len(b) / max(1, len(zlib.compress(b))) if b else 0.0


def looped(words: list[str]) -> bool:
    """Exact repeats (3+ of a 1-8 word unit in a row) or the 'same opening, new ending' template loop."""
    w = [_norm(x) for x in words if _norm(x)]
    for n in range(1, 9):
        for i in range(0, len(w) - 3 * n + 1):
            if w[i:i + n] == w[i + n:i + 2 * n] == w[i + 2 * n:i + 3 * n] and (n > 1 or len(w[i]) > 1):
                if n == 1 and (w[i].isdigit() or w[i] in SPELLED) and w[i:i + 5] != [w[i]] * 5:
                    continue          # "Sierra Sierra Sierra" / "seven seven seven" can be part of a real call
                return True
    clauses = [c.split() for c in re.split(r"[,.;!?]+", " ".join(words).lower()) if len(c.split()) >= 2]
    if len(clauses) >= 5:
        heads = {}
        for c in clauses:
            heads[tuple(c[:2])] = heads.get(tuple(c[:2]), 0) + 1
        if max(heads.values()) / len(clauses) >= 0.6:
            return True
    return False


def segment_ok(seg: dict, prompt: str = "") -> tuple[bool, str]:
    """Whisper's own failure signals on one decoded segment."""
    text = seg.get("text") or "".join(w["word"] for w in seg.get("words", []))
    if not text.strip():
        return False, "empty"
    if seg.get("compression_ratio", 0) > 2.4 or compression_ratio(text) > 2.4:
        return False, "loop"
    if looped(text.split()):
        return False, "loop"
    toks = text.split()
    bang = sum(1 for t in toks if not re.search(r"[A-Za-z0-9]", t))
    if bang >= 3 and bang >= 0.3 * len(toks):
        # "! ! ! ! ! ! ! ! ! ! H.R !.U. ! Mike": large-v3 lost on FM hiss (11-02 442.100 net); the words among the
        # bangs were no better
        return False, "garbage"
    # NOT dropped on no_speech_prob / avg_logprob: on the synthetic nets dropping those segments removed more right
    # words than wrong ones (WER 52.7 % loose vs 53.3 % strict, per-transmission 57.5 vs 59.5 %). Kept as flags.
    if prompt:
        p = " ".join(_norm(w) for w in prompt.split()[-12:])
        t = " ".join(_norm(w) for w in text.split())
        if len(p) > 12 and (p in t or (len(t) > 12 and t in p)):
            return False, "echo"
    return True, ""


def decode_windows(lay: Layout, wins, decode, prompt_prev=False) -> list[dict]:
    """Decode each window; return its words on the stitched timeline with their segment's verdict.
    words: {"w", "t0", "t1", "p", "win", "edge"} where edge = distance (s) to the nearest window CUT (a window end
    that is also the timeline's end or a real break does not count as a cut)."""
    total = len(lay.audio) / SR
    real = {round(t, 2) for t, s in lay.breaks if s == 2} | {0.0, round(total, 2)}
    out, prev_text = [], ""
    for k, (a, b) in enumerate(wins):
        x = lay.audio[int(a * SR):int(b * SR)]
        prompt = prev_text if prompt_prev else ""
        segs = decode(x, prompt) or []
        words = []
        for seg in segs:
            ok, why = segment_ok(seg, prompt)
            for w in seg.get("words", []):
                if not re.search(r"[A-Za-z0-9]", w["word"]):
                    continue                      # "!", "...": large-v3's filler on hiss carries no word
                t0, t1 = a + float(w["start"]), a + float(w["end"])
                ea = (t0 - a) if round(a, 2) not in real else 99.0
                eb = (b - t1) if round(b, 2) not in real else 99.0
                words.append({"w": w["word"], "t0": t0, "t1": t1, "p": float(w.get("probability", 0.0)),
                              "win": k, "edge": min(ea, eb), "ok": ok, "why": why})
        out.append({"a": a, "b": b, "words": words})
        good = [w["w"] for w in words if w["ok"]]
        prev_text = "".join(good).strip()[-200:]
    return out


def owner_bounds(lay: Layout, shift=JOIN_SHIFT_S) -> dict:
    """The stretch of timeline whose words belong to each clip. A pause split is cut at the END of the >= 0.8 s
    pause (station/segmenter.py yields when the pause is long enough), so the next piece's first word starts right
    at the join, and Whisper's word times run early by a few tenths of a second: a word ending a phrase is >= 0.8 s
    before the join, a word starting the next piece is within `shift` of it. So at a join the boundary moves
    `shift` earlier. At a real break (silence between transmissions) the clip edges stand."""
    out = {cid: list(lay.spans[cid]) for cid in lay.order}
    joins = {round(t, 3) for t, s in lay.breaks if s == 1}
    for prev, nxt in zip(lay.order, lay.order[1:]):
        j = lay.spans[nxt][0]
        if round(j, 3) in joins:
            cut = max(j - shift, lay.spans[prev][0])
            out[prev][1] = cut
            out[nxt][0] = cut
    out[lay.order[0]][0] = -1e9
    out[lay.order[-1]][1] = 1e9
    for prev, nxt in zip(lay.order, lay.order[1:]):     # separator silence: split it in the middle
        if out[prev][1] < out[nxt][0]:
            m = (out[prev][1] + out[nxt][0]) / 2
            out[prev][1] = out[nxt][0] = m
    return {k: tuple(v) for k, v in out.items()}


def _clip_words(words, a, b):
    return [w for w in words if a <= (w["t0"] + w["t1"]) / 2 < b]


def _score(ws):
    """Mean word probability with words near a window cut discounted."""
    if not ws:
        return 0.0
    return float(np.mean([w["p"] * (0.5 if w["edge"] < EDGE_S else 1.0) for w in ws]))


def merge(hyps: list[list[dict]]) -> list[dict]:
    """ROVER-style merge of two (or more) word sequences for the same stretch of audio: align them word by word;
    where they agree take the better-timed copy, where they differ take the side with the higher (edge-discounted)
    probability over the differing stretch."""
    hyps = [h for h in hyps if h is not None]
    if not hyps:
        return []
    base = hyps[0]
    for other in hyps[1:]:
        a, b = [_norm(w["w"]) for w in base], [_norm(w["w"]) for w in other]
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        out = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                for x, y in zip(base[i1:i2], other[j1:j2]):
                    out.append(x if x["edge"] >= y["edge"] else y)
            else:
                out += base[i1:i2] if _score(base[i1:i2]) >= _score(other[j1:j2]) else other[j1:j2]
        base = out
    return base


def assemble(lay: Layout, decoded: list[list[dict]], targets=None) -> dict:
    """decoded: one list of windows per tiling (decode_windows' output). -> per clip id:
    {"text", "words": [[word, t0, t1, p] in clip seconds], "conf", "flags"}."""
    res = {}
    bounds = owner_bounds(lay)
    for cid in lay.order:
        if targets is not None and cid not in targets:
            continue
        a, b = lay.spans[cid]
        oa, ob = bounds[cid]
        hyps, flags = [], set()
        for tiling in decoded:
            ws = [w for win in tiling for w in _clip_words(win["words"], oa, ob)]
            flags |= {w["why"] for w in ws if not w["ok"]}
            hyps.append([w for w in ws if w["ok"]])
        hyps.sort(key=_score, reverse=True)
        words = merge(hyps) if len(hyps) > 1 else (hyps[0] if hyps else [])
        words = sorted(words, key=lambda w: w["t0"])
        text = "".join(w["w"] for w in words).strip()
        text = re.sub(r"\s+", " ", text)
        conf = float(np.mean([w["p"] for w in words])) if words else 0.0
        if words and _norm_phrase(text) in NOISE_PHRASES and conf < 0.75:
            flags.add("noise_phrase"); words, text = [], ""
        if not words:
            flags.add("empty")
        elif conf < LOW_CONF:
            flags.add("low_conf")
        res[cid] = {"text": text, "conf": round(conf, 3), "flags": sorted(f for f in flags if f),
                    "words": [[w["w"].strip(), round(w["t0"] - a, 2), round(w["t1"] - a, 2), round(w["p"], 3)]
                              for w in words]}
    return res


def _norm_phrase(text: str) -> str:
    return " ".join(_norm(w) for w in text.split() if _norm(w))


def transcribe_clips(clips: list[Clip], decode, mode="both", phases=(0.0,), prompt_prev=False, max_s=WIN_MAX_S,
                     min_s=WIN_LONG_MIN_S) -> dict:
    """The whole method: stitch, decode, map back. Targets = clips with target=True (all of them if none is marked).
      "long"     the stitched block goes to the recogniser whole; Whisper's own sequential decode walks it 30 s at a
                 time, re-starting each window at the end of its last complete segment.
      "windows"  our own windows (min_s..max_s) cut at breaks, one tiling per phase.
      "both"     (the final pass) the two decodes above, whose window edges fall in different places, merged word
                 by word by confidence. Best measured: WER 50.4 % vs 52.7 % (long) / 52.8 % (windows) / 62.7 % (one
                 clip at a time) on the synthetic nets."""
    lay = stitch(clips)
    if not lay.order:
        return {}
    targets = {c.id for c in clips if c.target} or None
    decoded = []
    if mode in ("long", "both"):
        decoded.append(decode_windows(lay, [(0.0, len(lay.audio) / SR)], decode))
    if mode in ("windows", "both"):
        decoded += [decode_windows(lay, plan(lay, max_s=max_s, min_s=min_s, phase=ph), decode, prompt_prev)
                    for ph in phases]
    return assemble(lay, decoded, targets)
