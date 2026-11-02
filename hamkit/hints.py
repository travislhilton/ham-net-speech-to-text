"""Operator hints and net recognition from what was just said. Rules only, so it runs offline;
the LLM server LLM can refine net classification when online.

    h = Hinter(my_call="W9XYZ")
    h.on_transmission(text, callsigns, at=datetime)  -> {"cards": [...], "reminders": [...], "net": {...}}
"""
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from .glossary import ITU

CONFIG = Path(__file__).parent.parent / "config" / "procedures.yaml"
LETTER_WORD = {v: k.title() for k, v in ITU.items() if k not in ("ALPHA", "JULIETT", "WHISKY", "X-RAY")}
DIGIT = dict(zip("0123456789", "Zero One Two Three Four Five Six Seven Eight Nine".split()))

NET_TYPES = [  # (type, label, cue regexes) — first hit order matters only for ties
    ("emergency", "Possible REAL emergency", [r"\bmayday\b", r"\bemergency traffic\b", r"\breal (world|emergency)\b",
                                               r"\bnot a drill\b", r"\bno[- ]duff\b"]),
    ("exercise", "Exercise / drill", [r"\bthis is an? (exercise|drill)\b", r"\bexercise traffic\b", r"\bSET\b",
                                      r"\bsimulated\b"]),
    ("skywarn", "SKYWARN weather net", [r"\bskywarn\b", r"\bnational weather service\b", r"\bspotter\b",
                                        r"\bhail\b", r"\btornado\b", r"\bfunnel cloud\b"]),
    ("ares_races", "ARES / RACES net", [r"\bARES\b", r"\bRACES\b", r"\bemergency coordinator\b", r"\bEOC\b"]),
    ("traffic", "Traffic net (NTS / formal messages)", [r"\bradiogram\b", r"\bNTS\b", r"\bQTC\b", r"\bliaison\b",
                                                        r"\bformal traffic\b", r"\bICS[- ]?213\b"]),
    ("directed", "Directed net", [r"\bnet control\b", r"\bdirected net\b", r"\bpreamble\b", r"\bthis net\b"]),
    ("check_in", "Check-in net", [r"\bcheck[- ]?ins?\b", r"\bchecking in\b", r"\bfor the log\b"]),
    ("dx_contest", "DX / contest operating", [r"\bCQ (contest|test|DX)\b", r"\b5 ?9 ?9?\b", r"\bQRZ\b",
                                              r"\bsplit\b", r"\blistening (up|down)\b"]),
    ("ragchew", "Casual conversation (ragchew)", [r"\bragchew\b", r"\bQTH\b", r"\bthe weather here\b"]),
]


@lru_cache(maxsize=2)
def load(path: str = str(CONFIG)) -> dict:
    return yaml.safe_load(Path(path).read_text())


def phonetic(call: str) -> str:
    return " ".join(LETTER_WORD.get(c, DIGIT.get(c, c)) for c in call.upper())


def _hit(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", text, re.I) is not None


class Hinter:
    def __init__(self, my_call: str = "", procedures: dict | None = None, window: int = 30):
        self.me = my_call.upper()
        self.p = procedures or load()
        self.window = window
        self.recent: list[str] = []
        self.last_id: dict[str, datetime] = {}       # callsign -> last time it was heard identifying
        self.first_heard: dict[str, datetime] = {}

    def _fill(self, s: str, ncs: str | None) -> str:
        return (s.replace("{me_phonetic}", phonetic(self.me) if self.me else "<your call phonetically>")
                 .replace("{me}", self.me or "<your call>").replace("{ncs}", ncs or "net control"))

    def classify(self) -> dict:
        text = " ".join(self.recent)
        scores = {}
        for kind, label, cues in NET_TYPES:
            n = sum(len(re.findall(c, text, re.I)) for c in cues)
            if n:
                scores[kind] = (n, label)
        if not scores:
            return {"type": "unknown", "label": "Not yet recognized", "evidence": {}}
        # an emergency cue always wins; otherwise the strongest evidence
        kind = "emergency" if "emergency" in scores else max(scores, key=lambda k: scores[k][0])
        return {"type": kind, "label": scores[kind][1], "evidence": {k: v[0] for k, v in scores.items()}}

    def on_transmission(self, text: str, callsigns: list[str], at: datetime | None = None,
                        ncs: str | None = None) -> dict:
        at = at or datetime.now()
        self.recent = (self.recent + [text or ""])[-self.window:]
        for c in callsigns or []:
            self.first_heard.setdefault(c, at)
            self.last_id[c] = at
        cards = []
        for key, card in self.p["cards"].items():
            if any(_hit(text or "", t) for t in card["triggers"]):
                cards.append({"key": key, "title": card["title"], "priority": card.get("priority", "normal"),
                              "steps": card.get("steps", []),
                              "say": [self._fill(s, ncs) for s in card.get("say", [])]})
        cards.sort(key=lambda c: c["priority"] != "high")
        return {"cards": cards, "reminders": self.reminders(at), "net": self.classify()}

    def reminders(self, at: datetime) -> list[dict]:
        r = self.p["reminders"]["identify"]
        out = []
        if self.me and self.me in self.first_heard:
            since = at - self.last_id.get(self.me, self.first_heard[self.me])
            if since >= timedelta(seconds=r["warn_after_s"]):
                out.append({"key": "identify", "text": f"Identify soon: {self.me} last heard "
                            f"{int(since.total_seconds() // 60)} min ago. " + r["rule"]})
        return out
