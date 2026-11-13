"""The two reference cards (station/static/card.html, sidekick_card.html) as a list of steps and table rows.

The operator walkthrough (tools/operator_walkthrough.py) follows these in card order, and tests/test_card_in_sync.py
fails when a card changes without the walkthrough being looked at again: each item carries a short hash of its text.
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent.parent / "station" / "static"
CARDS = {"station": STATIC / "card.html", "sidekick": STATIC / "sidekick_card.html"}


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t or ""))).strip()


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def items(card: str) -> list[dict]:
    """[{id, card, section, part, kind: step|row, n, text, sha}] in card order. Ids: station:install.1, station:first.5,
    station:net.2, station:log.<cell>, station:wrong.<cell>, sidekick:setup.3, ..."""
    s = CARDS[card].read_text(encoding="utf-8")
    body = s[s.index("<body"):]
    out, section, part = [], "", ""
    pat = re.compile(r'<h2 class="card-title">(.*?)</h2>|<p class="card-part">(.*?)</p>|'
                     r'<div class="step-num">(\d+)</div><div class="step-text">(.*?)</div></div>|<tr><td>(.*?)</td><td>(.*?)</td></tr>', re.S)
    for m in pat.finditer(body):
        h2, p, num, step, c1, c2 = m.groups()
        if h2:
            section = clean(h2)
        elif p:
            part = clean(p)
        elif num:
            text = clean(step)
            out.append({"card": card, "section": section, "part": part, "kind": "step", "n": int(num), "text": text})
        else:
            out.append({"card": card, "section": section, "part": part, "kind": "row", "n": None,
                        "text": clean(c1) + " || " + clean(c2)})
    for it in out:
        it["slug"] = _slug(card, it)
        it["id"] = f"{card}:{it['slug']}"
        it["sha"] = sha(it["text"])
    ids = [it["id"] for it in out]
    assert len(ids) == len(set(ids)), "duplicate card item ids: " + ", ".join(i for i in ids if ids.count(i) > 1)
    return out


def _slug(card: str, it: dict) -> str:
    part = it["part"].lower()
    if card == "station":
        grp = ("install" if part.startswith("a ·") else "first" if part.startswith("b ·") else
               "net" if it["section"].lower().startswith("running") and it["kind"] == "step" else
               "log" if "reading" in part else "wrong" if "wrong" in part else "other")
    else:
        grp = ("setup" if it["section"].lower().startswith("set up") else
               "turn" if False else
               "chip" if "turn" in part and it["kind"] == "row" else "buttons" if part == "buttons" else
               "when" if part.startswith("when it speaks") else "wrong" if "wrong" in part else "net")
        if it["kind"] == "step" and it["section"].lower().startswith("set up"):
            grp = "setup"
        if part.startswith("the your turn"):
            grp = "chip"
        if it["section"].lower().startswith("sidekick on a net") and it["kind"] == "step":
            grp = "when"
    key = str(it["n"]) if it["kind"] == "step" else re.sub(r"[^a-z0-9]+", "-", it["text"].split("||")[0].lower()).strip("-")[:28]
    return f"{grp}.{key}"
