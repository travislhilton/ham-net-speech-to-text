"""The three reference cards (station/static/card.html, sidekick_card.html, conversation_card.html) as a list of steps
and table rows.

The operator walkthrough (tools/operator_walkthrough.py) follows these in card order, and tests/test_card_in_sync.py
fails when a card changes without the walkthrough being looked at again: each item carries a short hash of its text.
"""
from __future__ import annotations

import hashlib
import html
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent.parent / "station" / "static"
CARDS = {"station": STATIC / "card.html", "sidekick": STATIC / "sidekick_card.html",
         "conversation": STATIC / "conversation_card.html"}


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", t or ""))).strip()


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def items(card: str) -> list[dict]:
    """[{id, card, section, part, kind: step|row, n, text, sha}] in card order. Ids: station:install.1, station:first.5,
    station:net.2, station:log.<cell>, station:wrong.<cell>, sidekick:setup.3, conversation:done.7, ..."""
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


# Sidekick's card: each card part (its letter or title) -> the id group
SIDEKICK_PARTS = (("a ·", "switch"), ("b ·", "itself"), ("c ·", "ask"), ("the your turn", "chip"), ("set up", "setup"),
                  ("when something", "wrong"))
SIDEKICK_BUTTON_ROWS = ("i checked in myself", "mute for this net")
CONVERSATION_PARTS = (("a ·", "before"), ("b ·", "during"), ("c ·", "done"), ("find", "find"), ("use", "use"),
                      ("when something", "wrong"))


def _group(part: str, table) -> str:
    return next((g for p, g in table if part.startswith(p)), "other")


def _slug(card: str, it: dict) -> str:
    part = it["part"].lower()
    first = it["text"].split("||")[0].strip().lower()
    if card == "station":
        grp = ("install" if part.startswith("a ·") else "first" if part.startswith("b ·") else
               "net" if it["section"].lower().startswith("running") and it["kind"] == "step" else
               "log" if "reading" in part else "wrong" if "wrong" in part else "other")
    elif card == "sidekick":
        grp = _group(part, SIDEKICK_PARTS)
        if grp == "chip" and first in SIDEKICK_BUTTON_ROWS:
            grp = "buttons"
    else:
        grp = _group(part, CONVERSATION_PARTS)
    key = str(it["n"]) if it["kind"] == "step" else re.sub(r"[^a-z0-9]+", "-", first).strip("-")[:28]
    return f"{grp}.{key}"
