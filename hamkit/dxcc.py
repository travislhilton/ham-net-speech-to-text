"""Country (DXCC entity) for any callsign, offline, from AD1C's cty.dat (country-files.com).

Format: an entity line `Name: CQ: ITU: Cont: Lat: Lon(west+): UTC-offset(west+): Primary:` then
prefixes/exact calls (`=CALL`) separated by commas and ending with `;`. A prefix may carry
overrides: (CQ) [ITU] <lat/lon> {continent} ~utc~.

    python -m hamkit.dxcc update      # weekly-ish; keeps the previous file on failure
    python -m hamkit.dxcc lookup EA8AB VE3/W9XYZ JA1XYZ
"""
import json, os, re, sys
from functools import lru_cache
from pathlib import Path

URL = "https://www.country-files.com/bigcty/cty.dat"
DEFAULT = Path(os.environ.get("CALLBOOK_DIR", Path.home() / "data" / "callbook")) / "cty.dat"
_OV = re.compile(r"\((\d+)\)|\[(\d+)\]|<([-\d.]+)/([-\d.]+)>|\{(\w+)\}|~([-\d.]+)~")


def parse(text: str) -> tuple[dict, dict]:
    prefixes, exact = {}, {}
    ent = None
    for chunk in text.replace("\r", "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, rest = chunk.partition("\n") if ":" in chunk.split("\n", 1)[0] else ("", "", chunk)
        if head:
            f = [x.strip() for x in head.split(":")]
            ent = {"country": f[0], "cq": int(f[1]), "itu": int(f[2]), "continent": f[3],
                   "lat": float(f[4]), "lon": -float(f[5]), "utc_offset": -float(f[6]),
                   "primary_prefix": f[7].lstrip("*")}
        for item in re.split(r"[,\s]+", rest.strip()):
            if not item or ent is None:
                continue
            e = dict(ent)
            for m in _OV.finditer(item):
                if m.group(1): e["cq"] = int(m.group(1))
                if m.group(2): e["itu"] = int(m.group(2))
                if m.group(3): e["lat"], e["lon"] = float(m.group(3)), -float(m.group(4))
                if m.group(5): e["continent"] = m.group(5)
                if m.group(6): e["utc_offset"] = -float(m.group(6))
            key = _OV.sub("", item)
            (exact if key.startswith("=") else prefixes)[key.lstrip("=")] = e
    return prefixes, exact


@lru_cache(maxsize=2)
def _db(path: str = str(DEFAULT)):
    return parse(Path(path).read_text(encoding="latin-1"))


_SUFFIX = {"P", "M", "MM", "AM", "QRP", "A", "B", "R", "LH"}


def location_part(call: str) -> str:
    """The part of a portable call that decides the country: VE3/W9XYZ -> VE3, W9XYZ/P -> W9XYZ,
    W9XYZ/KH6 -> KH6, W9XYZ/4 -> W4 (a digit-only suffix changes the call area, not the country)."""
    parts = [p for p in call.upper().strip().split("/") if p]
    if len(parts) == 1:
        return parts[0]
    base = max(parts, key=len)
    others = [p for p in parts if p != base and p not in _SUFFIX]
    if not others:
        return base
    o = others[0]
    if o.isdigit():
        m = re.match(r"([A-Z0-9]*?[A-Z])\d", base)
        return (m.group(1) + o) if m else base
    return o


def lookup(call: str, path: str = str(DEFAULT)) -> dict | None:
    prefixes, exact = _db(path)
    c = call.upper().strip()
    if c in exact:
        return dict(exact[c], call=c, matched="=" + c)
    loc = location_part(c)
    if loc in exact:
        return dict(exact[loc], call=c, matched="=" + loc)
    for n in range(len(loc), 0, -1):
        if loc[:n] in prefixes:
            return dict(prefixes[loc[:n]], call=c, matched=loc[:n])
    return None


def update(path: Path = DEFAULT) -> int:
    from .fcc import _curl
    data = _curl(URL)
    p, e = parse(data.decode("latin-1"))
    if len(p) < 300:                                   # refuse a truncated or error page
        raise RuntimeError(f"cty.dat looks wrong ({len(p)} prefixes)")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_bytes(data); os.replace(tmp, path); _db.cache_clear()
    return len(p) + len(e)


if __name__ == "__main__":
    if sys.argv[1:2] == ["update"]:
        print(json.dumps({"entries": update()}))
    else:
        for c in sys.argv[2:]:
            print(json.dumps(lookup(c)))
