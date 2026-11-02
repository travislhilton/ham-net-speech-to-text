"""Find Q-codes, prowords and abbreviations in a transcript and say what they mean.

Whisper writes the same code several ways: "QRH", "Q R H", "Q-R-H", "Quebec Romeo Hotel", and
numbers as "73" / "seventy three" / "seven three". All of them map to one glossary entry."""
import re
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG = Path(__file__).parent.parent / "config" / "glossary.yaml"
ITU = {"ALFA": "A", "ALPHA": "A", "BRAVO": "B", "CHARLIE": "C", "DELTA": "D", "ECHO": "E", "FOXTROT": "F",
       "GOLF": "G", "HOTEL": "H", "INDIA": "I", "JULIET": "J", "JULIETT": "J", "KILO": "K", "LIMA": "L",
       "MIKE": "M", "NOVEMBER": "N", "OSCAR": "O", "PAPA": "P", "QUEBEC": "Q", "ROMEO": "R", "SIERRA": "S",
       "TANGO": "T", "UNIFORM": "U", "VICTOR": "V", "WHISKEY": "W", "WHISKY": "W", "XRAY": "X", "X-RAY": "X",
       "YANKEE": "Y", "ZULU": "Z"}
SPOKEN_NUMBERS = {"SEVENTY THREE": "73", "SEVEN THREE": "73", "EIGHTY EIGHT": "88", "EIGHT EIGHT": "88",
                  "FIVE NINE": "FIVE BY NINE", "5 BY 9": "FIVE BY NINE", "5 X 9": "FIVE BY NINE"}


@lru_cache(maxsize=4)
def load(path: str = str(CONFIG)) -> dict:
    return yaml.safe_load(Path(path).read_text())


_PHON = "|".join(sorted(ITU, key=len, reverse=True)).replace("-", r"\-")
_Q_PHONETIC = re.compile(rf"\bQUEBEC[\s,]+({_PHON})[\s,]+({_PHON})\b", re.I)
_Q_LETTERS = re.compile(r"(?<![A-Z0-9])Q[\s.\-]?([A-Z])[\s.\-]?([A-Z])(?![A-Z0-9])(\s*\?)?", re.I)


def find(text: str, glossary: dict | None = None) -> list[dict]:
    """Every glossary term heard in `text`, in order of first appearance, de-duplicated."""
    g = glossary or load()
    t = text or ""
    hits: dict[str, dict] = {}

    def add(term, kind, means, pos, question=False):
        if term not in hits:
            hits[term] = {"term": term, "kind": kind, "means": means, "pos": pos}
        if question:
            hits[term]["question"] = True

    for m in _Q_PHONETIC.finditer(t):
        code = "Q" + ITU[m.group(1).upper()] + ITU[m.group(2).upper()]
        if code in g["q_codes"]:
            add(code, "q_code", g["q_codes"][code]["means"], m.start())
    for m in _Q_LETTERS.finditer(t):
        code = ("Q" + m.group(1) + m.group(2)).upper()
        if code in g["q_codes"]:
            e = g["q_codes"][code]
            q = bool(m.group(3)) and "ask" in e
            add(code, "q_code", e["ask"] if q else e["means"], m.start(), q)
    up = re.sub(r"[^A-Z0-9 ?]", " ", t.upper())
    up = re.sub(r"\s+", " ", up)
    for spoken, canon in SPOKEN_NUMBERS.items():
        up = re.sub(rf"\b{re.escape(spoken)}\b", canon, up)
    for kind, table in (("proword", g["prowords"]), ("abbreviation", g["abbreviations"])):
        for term in sorted(table, key=len, reverse=True):      # longest first: BREAK BREAK before BREAK
            # a bare number must stand alone: "146.73" became "146 73" above and is a frequency
            num = str(term).isdigit()
            pat = (rf"(?<![0-9] )(?<![A-Z0-9]){re.escape(str(term))}(?! ?[0-9])(?![A-Z0-9])" if num
                   else rf"(?<![A-Z0-9]){re.escape(str(term).upper())}(?![A-Z0-9])")
            m = re.search(pat, up)
            if m and not any(term in h and term != h for h in hits):   # BREAK inside BREAK BREAK
                add(str(term), kind, table[term], m.start())
    return sorted(hits.values(), key=lambda h: h["pos"])
