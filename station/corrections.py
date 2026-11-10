"""What the operator's corrections teach (tap-to-correct on the dashboard: "✎ Fix").

A corrected transmission is ground truth twice over:

1. LEARNED FIX-UPS. The original text (what the station showed) is aligned with the corrected text word by word
   (station/wer.py align). A short substitution — at most 4 words heard, 3 said — that repeats (the same
   heard → say pair in at least two corrected transmissions), or that turns words into a callsign, is PROPOSED as a
   fix-up. Nothing is applied until the operator presses Accept: accepted fix-ups are written to
   data/fixups_learned.yaml in the `fixups:` format of config/vocab.yaml ({heard, say} plus count and first_at),
   which the post-transcription fix-up step reads beside the hand-written ones. Ignore hides a proposal for good
   (data/fixups_ignored.json); Remove takes an accepted one out of the file again.

2. ACCURACY. The word error rate of the live text, the accurate pass and (when there is one) the net-wide
   consensus text against the correction, per radio, mode and net. The correction is made FROM the text shown, so
   the operator's eye is on that text: small numbers (n under 20) are a rough guide, and the panel says so.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

import yaml

from . import wer

MAX_HEARD, MAX_SAY, MAX_HEARD_CALL = 4, 3, 6
MIN_REPEAT = 2
FEW = 20
CALLSIGN = re.compile(r"^(?:[A-Z]{1,2}\d[A-Z]{1,4}|\d[A-Z]\d[A-Z]{1,3})(?:/\w+|-\d+)?$")


def _tok(text: str) -> list[str]:
    """Words as written (punctuation at the ends trimmed)."""
    return [w for w in (re.sub(r"^[^\w/]+|[^\w/]+$", "", x) for x in (text or "").split()) if w]


def _key(w: str) -> str:
    return re.sub(r"[^a-z0-9/]", "", w.lower())


def substitutions(original: str, corrected: str) -> list[dict]:
    """The short word substitutions that turn `original` into `corrected`: [{heard, say, call}] (heard is
    lower-case as compared; say as the operator wrote it). Insertions and deletions alone are not fix-ups."""
    a, b = _tok(original), _tok(corrected)
    out = []
    for op, i0, i1, j0, j1 in wer.align([_key(x) for x in a], [_key(x) for x in b]):
        if op != "replace":
            continue
        heard = " ".join(_key(x) for x in a[i0:i1]).strip()
        say = " ".join(b[j0:j1]).strip()
        if not heard or not say or heard == _key(say):
            continue
        call = (j1 - j0) == 1 and bool(CALLSIGN.match(say.upper())) and say.upper() == say
        if (i1 - i0) > (MAX_HEARD_CALL if call else MAX_HEARD) or (j1 - j0) > MAX_SAY:
            continue
        out.append({"heard": heard, "say": say, "call": call})
    return out


def original_of(raw: dict) -> str:
    """The text the operator was looking at when they corrected it (kept in the correction), else the best one."""
    c = raw.get("corrected") or {}
    if c.get("from") is not None:
        return c["from"]
    return ((raw.get("accurate") or {}).get("text")) or raw.get("text") or ""


def proposals(rows: list[dict]) -> list[dict]:
    """Every heard → say pair in the corrections, counted once per transmission: {heard, say, count, call,
    first_at, examples, ready}. `ready` = repeats (count >= 2) or makes a callsign."""
    seen: dict[tuple, dict] = {}
    for raw in rows:
        c = raw.get("corrected") or {}
        if c.get("noise"):                         # marked Not speech: no words were corrected
            continue
        mine = set()
        for s in substitutions(original_of(raw), c.get("text") or ""):
            k = (s["heard"], s["say"])
            if k in mine:
                continue
            mine.add(k)
            p = seen.setdefault(k, {"heard": s["heard"], "say": s["say"], "count": 0, "call": s["call"],
                                    "first_at": c.get("at") or raw.get("start"), "examples": []})
            p["count"] += 1
            if len(p["examples"]) < 3:
                p["examples"].append({"id": raw.get("id"), "start": raw.get("start"), "text": c.get("text")})
    out = []
    for p in seen.values():
        p["ready"] = p["count"] >= MIN_REPEAT or p["call"]
        out.append(p)
    return sorted(out, key=lambda p: (-p["count"], p["first_at"] or ""))


class Learned:
    """data/fixups_learned.yaml (accepted) + data/fixups_ignored.json (ignored), both edited only by the operator."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "fixups_learned.yaml"
        self.ignored_path = Path(data_dir) / "fixups_ignored.json"

    def accepted(self) -> list[dict]:
        try:
            d = yaml.safe_load(self.path.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError):
            return []
        return [f for f in (d.get("fixups") or []) if isinstance(f, dict) and f.get("heard") and f.get("say")]

    def ignored(self) -> set[tuple]:
        try:
            return {tuple(x) for x in json.loads(self.ignored_path.read_text(encoding="utf-8"))}
        except (OSError, ValueError, TypeError):
            return set()

    def _write_yaml(self, fixups: list[dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        head = ("# Fix-ups the station learned from your corrections (tap-to-correct, then Accept in Learned fixes).\n"
                "# Same format as `fixups:` in config/vocab.yaml; edited by the dashboard, safe to edit by hand.\n")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(head + yaml.safe_dump({"fixups": fixups}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def _write_ignored(self, ign: set[tuple]):
        self.ignored_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ignored_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(list(x) for x in ign)), encoding="utf-8")
        os.replace(tmp, self.ignored_path)

    def view(self, rows: list[dict]) -> dict:
        acc = self.accepted()
        have = {(_key_phrase(f["heard"]), f["say"]) for f in acc}
        ign = self.ignored()
        props = [p for p in proposals(rows) if (p["heard"], p["say"]) not in have and (p["heard"], p["say"]) not in ign]
        return {"proposals": [p for p in props if p["ready"]], "watching": [p for p in props if not p["ready"]][:20],
                "accepted": acc, "ignored": len(ign), "corrections": len(rows), "file": str(self.path.name),
                "rule": f"A fix is proposed when the same words were corrected the same way in {MIN_REPEAT} "
                        f"transmissions, or once into a callsign. Nothing changes until you accept it."}

    def act(self, action: str, heard: str, say: str, rows: list[dict]) -> dict:
        heard, say = _key_phrase(heard), (say or "").strip()
        if not heard or not say or len(heard) > 80 or len(say) > 80:
            raise ValueError("heard and say are both needed (at most 80 characters)")
        acc = self.accepted()
        if action == "accept":
            p = next((x for x in proposals(rows) if x["heard"] == heard and x["say"] == say), None)
            if not any(_key_phrase(f["heard"]) == heard and f["say"] == say for f in acc):
                acc.append({"heard": heard, "say": say, "count": p["count"] if p else 1,
                            "first_at": (p or {}).get("first_at") or datetime.now().isoformat(timespec="seconds")})
                self._write_yaml(acc)
        elif action == "remove":
            self._write_yaml([f for f in acc if not (_key_phrase(f["heard"]) == heard and f["say"] == say)])
        elif action == "ignore":
            self._write_ignored(self.ignored() | {(heard, say)})
        elif action == "unignore":
            self._write_ignored(self.ignored() - {(heard, say)})
        else:
            raise ValueError("action is accept, ignore, unignore or remove")
        return self.view(rows)


def _key_phrase(s: str) -> str:
    return " ".join(_key(w) for w in str(s or "").split() if _key(w))


# ---------- accuracy against the corrections ----------
def _texts(raw: dict) -> dict:
    out = {"live": raw.get("text") or ""}
    a = raw.get("accurate")
    if a and a.get("text") is not None:
        out["accurate"] = a["text"]
    k = raw.get("consensus")                       # the net-wide consensus text, when the station has one
    if isinstance(k, dict) and k.get("text") is not None:
        out["consensus"] = k["text"]
    elif isinstance(k, str):
        out["consensus"] = k
    return out


def accuracy(rows: list[dict], me: str | None = None, radios: dict | None = None) -> dict:
    """WER of each text against the correction, overall and per radio / mode / net: errors / reference words
    (a pooled rate, so a long transmission counts for its length)."""
    radios = radios or {}
    rows = [r for r in rows if not (r.get("corrected") or {}).get("noise")]   # Not speech: nothing to score
    groups: dict[tuple, dict] = {}

    def add(kind, name, src, r):
        g = groups.setdefault((kind, name), {"by": kind, "name": name, "n": 0, "words": 0})
        if src == "live":
            g["n"] += 1; g["words"] += r["words"]
        s = g.setdefault(src, {"errors": 0, "words": 0, "n": 0})
        s["errors"] += r["errors"]; s["words"] += r["words"]; s["n"] += 1

    for raw in rows:
        ref = (raw.get("corrected") or {}).get("text") or ""
        rid = raw.get("radio") or "?"
        rname = (radios.get(rid) or {}).get("name") or raw.get("radio_name") or rid
        mode = raw.get("_mode") or (radios.get(rid) or {}).get("mode") or "?"
        for src, hyp in _texts(raw).items():
            r = wer.wer(ref, hyp, me)
            for kind, name in (("all", "All corrected lines"), ("radio", rname), ("mode", mode),
                               ("net", raw.get("_net") or "Conversations")):
                add(kind, name, src, r)
    out = []
    for g in groups.values():
        for src in ("live", "accurate", "consensus"):
            if src in g:
                g[src]["wer"] = round(g[src]["errors"] / max(g[src]["words"], 1), 3)
        g["few"] = g["n"] < FEW
        out.append(g)
    order = {"all": 0, "radio": 1, "mode": 2, "net": 3}
    out.sort(key=lambda g: (order[g["by"]], -g["n"], g["name"]))
    n = len(rows)
    return {"n": n, "groups": out, "few": n < FEW,
            "note": ("No corrected lines yet: fix a transmission (✎ Fix) and it is measured here." if not n else
                     f"Only {n} corrected line{'s' if n != 1 else ''}: a rough guide until there are {FEW} or more."
                     if n < FEW else "Measured against your corrections, which start from the text shown."),
            "rule": "Word error rate = words wrong, missing or extra, out of the words in your corrected text."}


def table(acc: dict) -> str:
    """The accuracy as plain text (tools/accuracy.py)."""
    pct = lambda s, g: (f"{s['wer'] * 100:.1f}%" + (f" ({s['n']})" if s["n"] != g["n"] else "")) if s else "—"
    lines = [f"Accuracy from your corrections: {acc['n']} corrected line{'s' if acc['n'] != 1 else ''}",
             acc["note"], acc["rule"], "",
             f"{'':6} {'':28} {'n':>4} {'live':>9} {'accurate':>12} {'consensus':>12}"]
    for g in acc["groups"]:
        lines.append(f"{g['by']:6} {g['name'][:28]:28} {g['n']:>4} {pct(g.get('live'), g):>9}"
                     f" {pct(g.get('accurate'), g):>12} {pct(g.get('consensus'), g):>12}"
                     f"{'  (few)' if g['few'] and g['by'] != 'all' else ''}")
    lines.append("")
    lines.append("(n) after a rate = it was measured on only n of the lines (the ones that have that text).")
    return "\n".join(lines)
