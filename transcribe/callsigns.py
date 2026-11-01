"""Normalize phonetically-spelled callsigns in Whisper output back to callsign strings.
'kilo seven alpha bravo charlie' -> 'K7ABC'. Also matches against the roster."""
import re, yaml
from pathlib import Path

PHON = {
 "alpha":"A","alfa":"A","bravo":"B","charlie":"C","delta":"D","echo":"E","foxtrot":"F","golf":"G",
 "hotel":"H","india":"I","juliet":"J","juliett":"J","kilo":"K","lima":"L","mike":"M","november":"N",
 "oscar":"O","papa":"P","quebec":"Q","romeo":"R","sierra":"S","tango":"T","uniform":"U","victor":"V",
 "whiskey":"W","x-ray":"X","xray":"X","yankee":"Y","zulu":"Z",
 "zero":"0","one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8",
 "nine":"9","niner":"9",
}
_pat = re.compile(r"\b((?:" + "|".join(PHON) + r")(?:[\s-]+(?:" + "|".join(PHON) + r")){3,6})\b", re.I)
_cs = re.compile(r"^[AKNW][A-Z]?\d[A-Z]{1,3}$")


def load_roster(path=Path(__file__).parent.parent / "config/vocab.yaml"):
    v = yaml.safe_load(open(path))
    return set(c.split("-")[0].upper() for c in v.get("roster", []))


def normalize(text: str, roster=None):
    found = []
    def rep(m):
        toks = re.split(r"[\s-]+", m.group(1).lower())
        cs = "".join(PHON[t] for t in toks)
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


def initial_prompt(path=Path(__file__).parent.parent / "config/vocab.yaml"):
    v = yaml.safe_load(open(path))
    return " ".join([v["area"], ", ".join(v["phrases"]) + ".",
                     "Tacticals: " + ", ".join(v["tacticals"]) + ".",
                     "Stations: " + ", ".join(v["roster"]) + "."])
