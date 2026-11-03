"""Normalize phonetically-spelled callsigns in Whisper output back to callsign strings.
'kilo seven alpha bravo charlie' -> 'K7ABC'. Also matches against the roster.

Vocabulary comes from config/vocab.yaml merged with the git-ignored config/local.yaml
(lists concatenated, scalars overridden) so a private roster never has to be committed."""
import re, yaml
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "config"

PHON = {
 "alpha":"A","alfa":"A","bravo":"B","charlie":"C","delta":"D","echo":"E","foxtrot":"F","golf":"G",
 "hotel":"H","india":"I","juliet":"J","juliett":"J","kilo":"K","lima":"L","mike":"M","november":"N",
 "oscar":"O","papa":"P","quebec":"Q","romeo":"R","sierra":"S","tango":"T","uniform":"U","victor":"V",
 "whiskey":"W","x-ray":"X","xray":"X","yankee":"Y","zulu":"Z",
 "zero":"0","one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8",
 "nine":"9","niner":"9",
}
# A run of 4-7 tokens, each a phonetic word or a bare digit (Whisper often writes "Kilo 7"),
# separated by spaces, hyphens or commas ("Kilo 7, Alpha Bravo Charlie").
_TOK = r"(?:" + "|".join(PHON) + r"|\d)"
_pat = re.compile(r"\b(" + _TOK + r"(?:[\s,-]+" + _TOK + r"){3,6})\b", re.I)
_cs = re.compile(r"^[AKNW][A-Z]?\d[A-Z]{1,3}$")
# Any amateur callsign shape (US and international): 1-3 prefix chars ending before a digit,
# the digit, then 1-4 letters. Validated by roster / FCC / country file, never trusted alone.
_ANY = re.compile(r"^(?=[A-Z0-9]{3,8}$)[A-Z0-9]{0,2}[A-Z]\d{1,2}[A-Z]{1,4}$|^\d[A-Z]\d[A-Z]{1,4}$")
# Whisper often writes the prefix and spells the rest: "WA7 X-ray Hotel Echo", "K0 X-ray Echo Tango", "K-Zero Mike…".
_LET = [w for w, v in PHON.items() if v.isalpha()]
_DIGW = [w for w, v in PHON.items() if v.isdigit()]
_MIX = re.compile(r"\b([AKNW][A-Z]?)[- ]?(\d|(?i:" + "|".join(_DIGW) + r"))((?:[\s,-]+(?i:" + "|".join(_LET) + r")){1,3})\b")
# Whisper splits written calls: "W9-XYZ", "W9 XYZ", "W-9-X-Y-Z", "W9-X-Y-Z".
_SPLIT = re.compile(r"\b([A-Z0-9]{1,3}(?:[-. ][A-Z0-9]{1,3}){1,5})\b")
# large-v3 hearing a whole turn writes the call as it is spoken, prefix and suffix apart: "KK7, XKG, Eli",
# "KB7, XLG Marvin", "KK-7, XKE" (11-02 442.100 net). Joined only when the joined call is known.
_COMMA = re.compile(r"\b([AKNW][A-Z]?)-?(\d),\s*([A-Z]{1,3})\b")


def load_vocab(config_dir: Path = CONFIG_DIR) -> dict:
    v = yaml.safe_load(open(config_dir / "vocab.yaml")) or {}
    local = config_dir / "local.yaml"
    if local.exists():
        for k, val in (yaml.safe_load(open(local)) or {}).items():
            if isinstance(val, list) and isinstance(v.get(k), list):
                v[k] = v[k] + [x for x in val if x not in v[k]]
            else:
                v[k] = val
    return v


def _base(cs: str) -> str:
    return cs.split("-")[0].upper()


def load_roster(config_dir: Path = CONFIG_DIR) -> set:
    v = load_vocab(config_dir)
    calls = set(_base(c) for c in v.get("roster", []))
    calls |= set(_base(r["callsign"]) for r in v.get("repeaters", []) if r.get("callsign"))
    return calls


def normalize(text: str, roster=None, validate=None):
    """Return (text with callsigns written plainly, callsigns found in order).
    Phonetic runs that form a US call are always taken. Written calls are taken when they are in
    `roster` or `validate(call)` is true (FCC database / known country prefix)."""
    found = []
    ok = lambda c: bool((roster and c in roster) or (validate and validate(c)))
    def rep(m):
        toks = re.split(r"[\s,-]+", re.sub(r"x-ray", "xray", m.group(1).lower()))   # "X-ray" crashed this (KeyError 'x')
        cs = "".join(t if t.isdigit() else PHON[t] for t in toks)
        if _cs.match(cs):
            found.append(cs)
            return cs
        return m.group(0)
    out = _pat.sub(rep, text)

    def mix(m):
        d = m.group(2) if m.group(2).isdigit() else PHON[m.group(2).lower()]
        letters = [PHON[t] for t in re.split(r"[\s,-]+", re.sub(r"x-ray", "xray", m.group(3).lower())) if t]
        cs = m.group(1) + d + "".join(letters)
        if _cs.match(cs) and (len(letters) >= 2 or ok(cs)):
            found.append(cs)
            return cs
        return m.group(0)
    out = _MIX.sub(mix, out)

    def join(m):
        s = m.group(1)
        if not any(ch.isdigit() for ch in s) or not re.search(r"[A-Z]", s):
            return s
        cs = re.sub(r"[-. ]", "", s)
        return cs if _ANY.match(cs) and ok(cs) else s
    def comma(m):
        cs = m.group(1) + m.group(2) + m.group(3)
        return cs if _ANY.match(cs) and ok(cs) else m.group(0)
    if roster or validate:
        out = _COMMA.sub(comma, out)
        out = _SPLIT.sub(join, out)
        for cs in re.findall(r"\b[A-Z0-9]{3,8}\b", out):
            if _ANY.match(cs) and cs not in found and ok(cs):
                found.append(cs)
    return out, found


def initial_prompt(config_dir: Path = CONFIG_DIR) -> str:
    """Whisper keeps only the tail of a long prompt, so the most valuable tokens go last."""
    v = load_vocab(config_dir)
    stations = sorted(set(_base(c) for c in v.get("roster", [])))
    reps = [f"{r['callsign']} repeater {r['output_mhz']}" for r in v.get("repeaters", []) if r.get("callsign")]
    parts = [v.get("area", "").strip(),
             ", ".join(v.get("phrases", [])) + ".",
             "Tacticals: " + ", ".join(v.get("tacticals", [])) + ".",
             "Repeaters: " + ", ".join(reps) + "." if reps else "",
             "Stations: " + ", ".join(stations) + "."]
    return " ".join(p for p in parts if p)
