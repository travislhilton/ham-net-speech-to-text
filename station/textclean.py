"""Whisper repetition-loop guard. On weak audio Whisper can repeat a phrase until it runs out of tokens
(seen on the test PC and the home server, 2026-11-02, a 20 s weak clip). We cap tokens by audio length and
collapse any phrase repeated 3+ times in a row to a single copy."""
import re


def max_tokens(duration_s: float) -> int:
    # fast speech is ~4 words/s, ~1.4 tokens/word; generous headroom, hard ceiling at Whisper's 448
    return int(min(440, 24 + duration_s * 8))


def collapse_repeats(text: str, min_repeats: int = 3) -> tuple[str, bool]:
    words = (text or "").split()
    out, i, looped = [], 0, False
    while i < len(words):
        hit = False
        for n in range(1, 9):
            unit = [w.lower().strip(".,!?") for w in words[i:i + n]]
            if len(unit) < n:
                break
            k = 1
            while [w.lower().strip(".,!?") for w in words[i + k * n:i + (k + 1) * n]] == unit:
                k += 1
            if k >= min_repeats:
                out += words[i:i + n]; i += k * n; hit = looped = True
                break
        if not hit:
            out.append(words[i]); i += 1
    s = " ".join(out)
    return (re.sub(r"\s+([.,!?])", r"\1", s), looped)


def template_loop(text: str, min_clauses: int = 5, share: float = 0.6) -> bool:
    """Whisper's other loop on weak audio: the same opening over and over with new endings ("I'm a lawyer, I'm a
    tester, I'm a computer, I'm a computer..."). Exact repeats are caught by collapse_repeats; this is the rest."""
    import re
    clauses = [c.split() for c in re.split(r"[,.;!?]+", (text or "").lower()) if len(c.split()) >= 2]
    if len(clauses) < min_clauses:
        return False
    heads = {}
    for c in clauses:
        heads[tuple(c[:2])] = heads.get(tuple(c[:2]), 0) + 1
    return max(heads.values()) / len(clauses) >= share
