"""Where a US licence is: the centre of its mailing ZIP code (US Census ZCTA gazetteer, public domain, ~1 MB,
33k ZIPs), and distances from the station. Used on VHF/UHF, where the stations a net hears are the ones in range
(station/consensus.py: a clipped or doubtful call goes to the nearby licence, not the one across the state).

  python -m hamkit.zipgeo update      download the gazetteer into the callbook folder (once; the installer does it)
  python -m hamkit.zipgeo where 62701 the ZIP's centre

A mailing address is not always where the station is (a PO box, a winter home, a mobile) — distance is evidence,
never proof: it ranks licences that fit what was heard, it does not invent one."""
from __future__ import annotations

import argparse
import io
import math
import re
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path

from .fcc import DEFAULT_DB

URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip"
DEFAULT_GEO = DEFAULT_DB.parent / "zipgeo.sqlite"


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km between (lat, lon) points."""
    (la1, lo1), (la2, lo2) = a, b
    r = math.radians
    x = math.sin(r(la2 - la1) / 2) ** 2 + math.cos(r(la1)) * math.cos(r(la2)) * math.sin(r(lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, x)))


def grid_to_latlon(grid: str) -> tuple[float, float] | None:
    """The centre of a Maidenhead locator (EM59 = 1 x 2 degrees; EM59et = 2.5 x 5 minutes)."""
    g = (grid or "").strip()
    if not re.fullmatch(r"[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?", g):
        return None
    g = g[:2].upper() + g[2:4] + g[4:].lower()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - 97) * (2 / 24) + 1 / 24
        lat += (ord(g[5]) - 97) * (1 / 24) + 1 / 48
    else:
        lon += 1
        lat += 0.5
    return (lat, lon)


class ZipGeo:
    """ZIP -> (lat, lon), all in memory (33k rows)."""
    def __init__(self, path: Path = DEFAULT_GEO, rows: dict | None = None):
        self.path = Path(path)
        if rows is not None:
            self.z = dict(rows)
        elif self.path.exists():
            con = sqlite3.connect(self.path)
            try:
                self.z = {z: (la, lo) for z, la, lo in con.execute("SELECT zip, lat, lon FROM zip")}
            finally:
                con.close()
        else:
            self.z = {}

    def __bool__(self):
        return bool(self.z)

    def where(self, zipcode: str | None) -> tuple[float, float] | None:
        return self.z.get((zipcode or "").strip()[:5]) if zipcode else None

    def region(self, zipcode: str | None) -> tuple[float, float] | None:
        """The centre of the ZIP's 3-digit region, for a ZIP with no centre of its own: a PO-box ZIP (2.1% of active
        licences, 2026-11-10; 97% have a region). Up to ~200 km out in the West, so it can only tell FAR."""
        z3 = (zipcode or "").strip()[:3]
        if len(z3) != 3:
            return None
        if not hasattr(self, "_z3"):
            acc: dict[str, list] = {}
            for z, (la, lo) in self.z.items():
                a = acc.setdefault(z[:3], [0.0, 0.0, 0])
                a[0] += la; a[1] += lo; a[2] += 1
            self._z3 = {k: (a[0] / a[2], a[1] / a[2]) for k, a in acc.items()}
        return self._z3.get(z3)


def parse(text: str) -> dict[str, tuple[float, float]]:
    out = {}
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) >= 7 and p[0].strip().isdigit():
            try:
                out[p[0].strip()] = (float(p[5]), float(p[6]))
            except ValueError:
                continue
    return out


def update(path: Path = DEFAULT_GEO, url: str = URL, log=print) -> int:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ham-net-speech-to-text"}),
                                timeout=120) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.endswith(".txt"))
        rows = parse(z.read(name).decode("utf-8", "replace"))
    if len(rows) < 10000:
        raise RuntimeError(f"the gazetteer had only {len(rows)} ZIPs")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE zip (zip TEXT PRIMARY KEY, lat REAL, lon REAL)")
    con.executemany("INSERT INTO zip VALUES (?, ?, ?)", [(z, la, lo) for z, (la, lo) in rows.items()])
    con.commit()
    con.close()
    tmp.replace(path)
    log(f"zipgeo: {len(rows)} ZIP centres -> {path}")
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hamkit.zipgeo")
    ap.add_argument("--db", default=str(DEFAULT_GEO))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update")
    w = sub.add_parser("where")
    w.add_argument("zip")
    a = ap.parse_args(argv)
    if a.cmd == "update":
        if Path(a.db).exists():
            print(f"zipgeo: already have {a.db} (delete it to download again)")
            return 0
        update(Path(a.db))
        return 0
    print(ZipGeo(Path(a.db)).where(a.zip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
