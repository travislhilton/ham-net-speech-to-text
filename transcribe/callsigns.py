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


def normalize(text: str, roster=None):
    found = []
    def rep(m):
        toks = re.split(r"[\s,-]+", m.group(1).lower())
        cs = "".join(t if t.isdigit() else PHON[t] for t in toks)
        if _cs.match(cs):
            found.append(cs)
            return cs
        return m.group(0)
    out = _pat.sub(rep, text)
    if roster:
        for cs in re.findall(r"\b[AKNW][A-Z]?\d[A-Z]{1,3}\b", out):
            if cs in roster and cs not in found:
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
