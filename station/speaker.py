"""Who is talking on a transmission. Four sources, best first:

  operator  the operator picked or typed it on the dashboard ("Who?"); always wins
  stated    the transmission identifies its own station: "this is K7ABC", "K7ABC here", "de K7ABC",
            "…K7ABC, over/clear", "K7ABC, Springfield, no traffic" (a check-in), "W9XYZ, K7ABC, go ahead"
  ncs       no station identified itself, but the words are net control's (a read-back or acknowledgement:
            "K7ABC, thank you, you're in the log") and the schedule names net control for this net
  voice     the voice matches, clearly, a voice that did identify itself earlier in the same net
            (station/voiceprint.py measures; `backfill` below decides)

A callsign that is only being ADDRESSED ("K7ABC, thank you", "roger K7ABC", "go ahead K7ABC", "number 12,
K7ABC", "let's try K7ABC") is never the speaker. Rules only: offline, explainable, testable.

A transmission keeps `speaker` (the best call or None, what the forms and the dashboard use) and `speaker_how`
(which of the four), plus `speaker_sim` for a voice match.
"""
import re

import numpy as np

RANK = {"operator": 4, "stated": 3, "ncs": 2, "voice": 1}

_C = r"(?P<c>[A-Z0-9]{1,3}\d[A-Z0-9/-]{1,8})"           # a callsign as normalize() writes it (checked against `calls`)

# The station identifying itself.
_SELF = [
    re.compile(r"\b(?:this is|this be|it'?s|here is|de|delta echo)\s+" + _C, re.I),
    re.compile(_C + r"[,.]?\s+(?:here|calling|checking in|check(?:ing)? in|for check-?in|for the net|"
               r"with(?:out)? (?:no )?traffic|no traffic|nothing for the net|mobile|portable|fixed)\b", re.I),
    re.compile(r"\b[A-Z0-9]{1,3}\d[A-Z0-9/-]{1,8}[,.]?\s+(?:this is\s+|de\s+)?" + _C
               + r"[,.]?\s+(?:go ahead|over|k)\W*$", re.I),            # "THEIR call, MY call, go ahead"
    re.compile(_C + r"[,.]?\s+(?:over|clear|out|clear and listening|listening|seventy[- ]three|73)\W*$", re.I),
]

# The callsign being spoken TO (net control's read-backs and acknowledgements).
_TO = [
    re.compile(_C + r"[,.]?\s+(?:thank(?:s| you)|you'?re in|you are in|in the log|logged|i have you|i got you|"
               r"got you|good (?:morning|afternoon|evening|night)|welcome|go ahead|roger|copy|qsl|"
               r"(?:you'?re |you are )?(?:loud|five|5) (?:and|by|&) (?:clear|nine|9)|please (?:go|stand))", re.I),
    re.compile(r"\b(?:thank(?:s| you)(?: (?:for (?:the|your) (?:check-?in|call|report)))?|roger|copy|qsl|welcome|"
               r"go ahead|good (?:morning|afternoon|evening)|got you|got|have you|let'?s try|try|calling|"
               r"check-?in from|number \d+|that'?s|for)[,.]?\s+(?:to\s+)?" + _C, re.I),
]

# Words that are net control's: acknowledging, logging, handing the floor.
_ACK = re.compile(r"\b(?:you'?re in|you are in|got you in|have you in|in the log|logged|welcome (?:aboard|in|to)|(?:signal|copy|check-?in),? welcome\b|"
                  r"thanks? (?:you )?for (?:your|the) check-?in|have a (?:\w+ )?(?:good|great|terrific|wonderful|"
                  r"splendid|grand|fine|nice|spectacular|fabulous) (?:evening|night|day|morning|afternoon)|"
                  r"(?:in )?without (?:any )?traffic|any (?:other )?check-?ins|next check-?in|"
                  r"that brings us to|go ahead with your traffic|any late or missed)\b", re.I)

_WORD = re.compile(r"[A-Za-z0-9']+")


def addressed(text: str, calls: list[str]) -> set[str]:
    out = set()
    for p in _TO:
        for m in p.finditer(text or ""):
            c = m.group("c").upper()
            if c in calls:
                out.add(c)
    return out


def stated(text: str, calls: list[str]) -> str | None:
    """The callsign this transmission identifies as its own, or None."""
    if not calls or not text:
        return None
    to = addressed(text, calls)
    for i, p in enumerate(_SELF):
        for m in p.finditer(text):
            c = m.group("c").upper()
            if c in calls and (i in (0, 2) or c not in to):     # "this is X", "THEIR, MY, go ahead" win over everything
                return c
    # A bare check-in: the transmission starts with a callsign and is little more than that
    # ("KE7XHT, Ogdenville." / "W7ETQ Capital City. W7ETQ.") and nothing in it acknowledges anyone.
    # Not when it names several stations (net control reading back the check-ins: "KI7ABC, KI7ABD, KJ7ABE.";
    # "KK7ABC. … great signal, welcome. KE7ABD." is KE7ABD's acknowledgement) or asks something ("K7ABC, do I got it right? W9XYZ." is W9XYZ checking the call it heard) — 2026-11-07, FM nets: such
    # transmissions were shown as the first station's own.
    first = re.match(r"\W*" + _C, text)
    named = {c.upper() for c in calls if re.search(r"\b" + re.escape(c) + r"\b", text, re.I)}
    # ... but two stations checking in together ("KF7ABC and KF7ABD.", a couple or family): the first is the speaker
    pair = re.fullmatch(r"\W*" + _C + r"\s*,?\s+and\s+[A-Z0-9]{3,8}\W*(?:\s*\1\s+and\s+[A-Z0-9]{3,8}\W*)?", text, re.I)
    several = len(named) > 1 and not pair
    if (first and first.group("c").upper() in calls and first.group("c").upper() not in to and not _ACK.search(text)
            and not several and "?" not in text):
        rest = [w for w in _WORD.findall(text[first.end():]) if w.upper() not in calls]
        if len(rest) <= 5:
            return first.group("c").upper()
    return None


def from_text(text: str, calls: list[str], ncs: str | None = None) -> tuple[str | None, str | None]:
    """(speaker, how) from the words alone: ('K7ABC', 'stated'), ('W9XYZ', 'ncs') or (None, None)."""
    s = stated(text, calls)
    if s:
        return s, "stated"
    ncs = (ncs or "").upper() or None
    if ncs and text and (_ACK.search(text) or addressed(text, calls)):
        return ncs, "ncs"
    return None, None


def best(tx: dict) -> tuple[str | None, str | None]:
    """The speaker the forms and the dashboard should use: operator > stated (the corrected text's, the accurate
    text's, then the live text's) > net control > voice."""
    if tx.get("speaker_set"):
        return tx["speaker_set"], "operator"
    cands = []
    for src in (tx.get("corrected") or {}, tx.get("accurate") or {}, tx):     # the operator's corrected text first
        if not src:
            continue
        if "speaker_how" in src:
            who, how = src.get("speaker"), src.get("speaker_how")
        else:                     # logged before speaker_how existed: its `speaker` was a last-callsign guess
            who, how = from_text(src.get("text") or "", src.get("callsigns") or [])
        if who and how in RANK:
            cands.append((RANK[how], who, how))
    if tx.get("speaker_voice"):
        cands.append((RANK["voice"], tx["speaker_voice"]["call"], "voice"))
    if not cands:
        return None, None
    _, call, how = max(cands, key=lambda c: c[0])                # max keeps the first of equals: corrected, accurate
    return call, how


# ---------- voice backfill ----------
def backfill(items: list[dict], threshold: float = 0.6, margin: float = 0.08) -> dict[str, dict]:
    """items: [{"id", "emb": unit vector or None, "call": anchor callsign or None}], one net or conversation.
    Anchors are transmissions whose speaker is operator-set or stated. Each transmission without a call gets the
    anchor call whose closest voice is at least `threshold` (cosine) AND beats every other call's by `margin`;
    returns {id: {"call", "sim"}}. Nothing is chained: a voice-inferred transmission never becomes an anchor."""
    anchors: dict[str, list[np.ndarray]] = {}
    for it in items:
        if it.get("call") and it.get("emb") is not None:
            anchors.setdefault(it["call"], []).append(np.asarray(it["emb"], np.float32))
    if not anchors:
        return {}
    calls = list(anchors)
    out = {}
    for it in items:
        if it.get("call") or it.get("emb") is None:
            continue
        e = np.asarray(it["emb"], np.float32)
        sims = sorted(((max(float(a @ e) for a in anchors[c]), c) for c in calls), reverse=True)
        top, call = sims[0]
        second = sims[1][0] if len(sims) > 1 else -1.0
        if top >= threshold and top - second >= margin:
            out[it["id"]] = {"call": call, "sim": round(top, 3)}
    return out
