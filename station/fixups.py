"""Local-name fix-ups AFTER transcription: words Whisper keeps mishearing on this station ("Aries" for ARES,
"Windlink" for Winlink, the net's own name). They are never put in Whisper's prompt: a prompt full of local
names made Whisper echo the prompt instead of the audio (2026-11-02).

Rules come from three places, merged in this order (a later rule for the same `heard` replaces an earlier one):

    config/vocab.yaml        `fixups:`  generic radio words (committed)
    config/local.yaml        `fixups:`  the owner's area names, club and net names (git-ignored)
    data/fixups_learned.yaml            pairs learned from the operator's corrections on the dashboard

Each rule is {heard: "...", say: "...", context: optional regex}. `heard` matches whole words, any case;
`context` must match somewhere else in the same sentence (case-insensitive) or the rule is skipped, so
"Aries" becomes ARES in "the Aries net" but not in "the constellation Aries". A plain mapping {heard: say} is
accepted too. data/fixups_learned.yaml may be that list, or {fixups: [...]}.

The scheduled net's own name is also repaired by sound: "the Springfeld Country Aries net" -> "the Springfield
County ARES net" (sound-alike score of the whole phrase, conservative threshold, the phrase must end in "net" and
start with a sound-alike word). Spoken names come from the schedule entry (`spoken:` list, else its name when it
has 2+ words) and `net_names:` in local.yaml.

apply() returns the new text and what changed: [{"heard", "say", "why"}]. The caller keeps the original text."""
import re
from difflib import SequenceMatcher
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
NET_THRESHOLD = 0.84          # sound-alike similarity a near-miss net name must reach


def _rules_from(obj, source: str) -> list[dict]:
    out = []
    if isinstance(obj, dict):
        obj = [{"heard": k, "say": v} for k, v in obj.items()]
    for r in obj or []:
        if isinstance(r, dict) and r.get("heard") and r.get("say") is not None:
            out.append({"heard": str(r["heard"]).strip(), "say": str(r["say"]).strip(),
                        "context": r.get("context") or None, "source": source})
    return out


def _yaml(p: Path):
    try:
        return yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def load(config_dir: Path = ROOT / "config", data_dir: Path | None = ROOT / "data") -> dict:
    """{"rules": [...], "net_names": [...]} from vocab.yaml + local.yaml + data/fixups_learned.yaml."""
    vocab, local = _yaml(Path(config_dir) / "vocab.yaml"), _yaml(Path(config_dir) / "local.yaml")
    learned = _yaml(Path(data_dir) / "fixups_learned.yaml") if data_dir else {}
    if isinstance(learned, dict) and "fixups" in learned:
        learned = learned["fixups"]
    merged: dict[str, dict] = {}
    for obj, src in ((vocab.get("fixups"), "vocab"), (local.get("fixups"), "local"), (learned, "learned")):
        for r in _rules_from(obj, src):
            merged[r["heard"].lower()] = r
    names = [str(n) for n in (vocab.get("net_names") or []) + (local.get("net_names") or []) if n]
    return {"rules": list(merged.values()), "net_names": list(dict.fromkeys(names))}


def net_names_for(net: dict | None, extra=()) -> list[str]:
    """Spoken forms of a scheduled net's name: its `spoken:` list, its name when it is 2+ words, `extra`."""
    out = []
    if net:
        sp = net.get("spoken") or []
        out += [str(s) for s in ([sp] if isinstance(sp, str) else sp)]
        if len(str(net.get("name") or "").split()) >= 2:
            out.append(str(net["name"]))
    out += list(extra)
    return list(dict.fromkeys(n for n in out if n))


def _cased(match: str, say: str) -> str:
    """A `say` written with capitals (ARES, Winlink) is used as written, in capitals when the heard word was;
    an all-lower-case `say` (a learned pair) takes the heard word's case."""
    if match.isupper() and len(match) > 1:
        return say.upper()
    if say != say.lower():
        return say
    if match[:1].isupper():
        return say[:1].upper() + say[1:]
    return say


_SENT = re.compile(r"[^.!?]+[.!?]*")


def _sentence_of(text: str, pos: int) -> tuple[int, int]:
    for m in _SENT.finditer(text):
        if m.start() <= pos < m.end():
            return m.span()
    return 0, len(text)


def apply_rules(text: str, rules: list[dict]) -> tuple[str, list[dict]]:
    changes: list[dict] = []
    for r in rules:
        # whole words, any case; punctuation between the words is ignored ("Wind, link"), like the learned pairs,
        # which are stored lower case without punctuation
        words = re.findall(r"[\w'.]+", r["heard"])
        words = [w.strip(".") or w for w in words] if len(words) > 1 else words
        if not words:
            continue
        pat = re.compile(r"(?<![\w-])" + r"[\s,;:.!?-]+".join(map(re.escape, words)) + r"(?![\w-])", re.I)
        if not pat.search(text):
            continue
        ctx = None
        if r.get("context"):
            try:
                ctx = re.compile(r["context"], re.I)
            except re.error:
                continue
        src = text

        def sub(m, ctx=ctx, r=r, src=src):
            if ctx is not None:
                s, e = _sentence_of(src, m.start())
                around = src[s:m.start()] + " " + src[m.end():e]          # the context is AROUND the word
                if not ctx.search(around):
                    return m.group(0)
            new = _cased(m.group(0), r["say"])
            if new != m.group(0):
                changes.append({"heard": m.group(0), "say": new, "why": f"fix-up ({r['source']})"})
            return new
        text = pat.sub(sub, text)
    return text, changes


# ---------- the net's own name, by sound ----------
def _key(word: str) -> str:
    """A rough sound key: spellings that sound alike collapse (ph/f, c/k/s, doubled letters, vowels after the
    first letter)."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("z", "s"), ("dg", "j"), ("gh", "g"),
                 ("tch", "ch"), ("wh", "w")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    w = w[0] + re.sub(r"[aeiouyhw]", "", w[1:])
    return re.sub(r"(.)\1+", r"\1", w)


def similarity(a: list[str], b: list[str]) -> float:
    """Sound-alike similarity of two phrases (0..1): the better of the sound keys and the spelling."""
    ka, kb = " ".join(_key(x) for x in a), " ".join(_key(x) for x in b)
    if not (ka and kb):
        return 0.0
    letters = SequenceMatcher(None, " ".join(a).lower(), " ".join(b).lower()).ratio()
    return max(SequenceMatcher(None, ka, kb).ratio(), letters)


_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def repair_net_name(text: str, names: list[str], threshold: float = NET_THRESHOLD) -> tuple[str, list[dict]]:
    """Replace a near-miss of a spoken net name that ends in the word "net" ("Springfeld Country Aries net")."""
    changes: list[dict] = []
    for name in names:
        target = name.split()
        if target[-1].lower() != "net":
            target = target + ["net"]
        if len(target) < 3:                       # "<one word> net" is too short to judge by sound
            continue
        words = list(_WORD.finditer(text))
        for j, w in enumerate(words):
            if w.group(0).lower() != "net":
                continue
            best, exact = None, False
            for n in (len(target),):                 # as many words as the name: never adds or drops a word
                i = j - n + 1
                if i < 0:
                    continue
                span = words[i:j + 1]
                if any(text[a.end():b.start()].strip() for a, b in zip(span, span[1:])):
                    continue                      # the phrase must run without punctuation between words
                got = [x.group(0) for x in span]
                if [g.lower() for g in got[-len(target):]] == [t.lower() for t in target]:
                    exact = True
                    break
                s = similarity(got, target)
                # its first word must sound alike too: "the club net" never becomes another net's name
                if s >= threshold and _key(got[0])[:2] == _key(target[0])[:2] and (best is None or s > best[0]):
                    best = (s, span[0].start(), span[-1].end(), " ".join(got))
            if best and not exact:
                s, a, b, heard = best
                say = " ".join(target[:-1]) + " " + w.group(0)
                text = text[:a] + say + text[b:]
                changes.append({"heard": heard, "say": say,
                                "why": f"the scheduled net's name (sounds {s:.0%} alike)"})
                break                              # one repair per name per transmission; offsets moved
    return text, changes


def apply(text: str, cfg: dict, net_names=()) -> tuple[str, list[dict]]:
    """Fix-up rules, then the net's own name. -> (text, changes)."""
    if not text:
        return text, []
    t, ch = apply_rules(text, cfg.get("rules") or [])
    names = list(dict.fromkeys(list(net_names) + list(cfg.get("net_names") or [])))
    if names:
        t, ch2 = repair_net_name(t, names)
        ch += ch2
    return t, ch
