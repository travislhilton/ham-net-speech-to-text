"""VHF/UHF radio path analysis over real terrain: can site A reach site B, how well, and what would fix it.

Built for Sidekick (the station's assistant) to call as a tool when it is asked to plan a net or Winlink links
("analyze the paths between the EOC and the shelters"): the model must not guess terrain, so every number it gives
comes from here.

  * Terrain: ground elevation sampled along the great-circle path (default every 100 m, at most 600 samples) from
    the USGS Elevation Point Query Service (3DEP, US only; source="usgs", the default) or Open-Meteo (Copernicus
    90 m, worldwide, 100 points a query: source="open-meteo"), cached on disk in the callbook folder next to the ZIP
    centres (hamkit/zipgeo.py). No network = an ElevationError, never a silent flat earth or another source.
  * Geometry: effective earth radius k = 4/3 (standard refraction), the straight ray between the two antennas, the
    first Fresnel zone and how much of it the terrain leaves clear.
  * Loss: free-space path loss + knife-edge diffraction (ITU-R P.526: one edge, or Deygout with up to 3 edges).
  * Link budget: transmitter power, cable loss, antenna gains, receiver sensitivity per mode -> received level and
    fade margin for FM voice, VARA FM and 1200-baud packet.

Terrain only: buildings, trees and the inside of a building are not in it (they add roughly 5-20 dB at VHF/UHF;
pass clutter_db to allow for them). Ducting, troposcatter and repeater coverage are not modelled.

  python -m hamkit.pathprofile 40.0150,-105.2705 39.9990,-105.2950 --mhz 146 --ha 10 --hb 6 --watts 50
  python -m hamkit.pathprofile DN70la DN70kb --json --source open-meteo

Public API: Point, point(), analyze(), text(), matrix(), best_relay(), height_needed(), elevations(),
ElevationError.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .fcc import DEFAULT_DB
from .zipgeo import grid_to_latlon, km

EPQS_URL = "https://epqs.nationalmap.gov/v1/json?x={lon:.6f}&y={lat:.6f}&units=Meters&wkid=4326&includeDate=false"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}"
DEFAULT_CACHE = DEFAULT_DB.parent / "elevation.sqlite"
EARTH_R_M = 6371000.0
C_M_S = 299792458.0
FT = 0.3048

# Receiver sensitivity (dBm) for a usable signal. FM voice: 12 dB SINAD on a typical mobile (~0.22 uV).
# VARA FM and 1200-baud packet need a cleaner signal: about 10 dB more.
SENS_VOICE_DBM = -120.0
SENS_VARA_DBM = -110.0
SENS_PACKET_DBM = -110.0
MODES = (("voice", "FM voice"), ("vara_fm", "VARA FM"), ("packet", "1200-baud packet"))


class ElevationError(RuntimeError):
    """The terrain could not be read (the service unreachable, or no data at a point: outside the US, the sea)."""


# --------------------------------------------------------------------------------------------------- points

@dataclass
class Point:
    """A site: where it is, how high its antenna is above the ground there, optionally its name and antenna gain."""
    lat: float
    lon: float
    height_m: float = 10.0
    name: str | None = None
    gain_dbi: float | None = None        # None = the gain passed to analyze()

    @property
    def label(self) -> str:
        return self.name or f"{self.lat:.4f},{self.lon:.4f}"


def point(spec, height: float | None = None, unit: str = "m", name: str | None = None,
          gain_dbi: float | None = None) -> Point:
    """A Point from what a person or a model would write: "40.015,-105.27", a Maidenhead grid ("DN70la" = its
    centre: a 6-character grid is ~5 x 4 km, so give coordinates for a real analysis), a (lat, lon) tuple, a dict
    ({lat, lon} or {grid}, with height_m / height_ft / name / gain_dbi), or a Point. height is in `unit` (m or ft)."""
    if isinstance(spec, Point):
        p = Point(spec.lat, spec.lon, spec.height_m, spec.name, spec.gain_dbi)
    elif isinstance(spec, dict):
        if spec.get("grid"):
            p = point(spec["grid"])
        else:
            p = Point(float(spec["lat"]), float(spec["lon"]))
        if spec.get("height_m") is not None:
            p.height_m = float(spec["height_m"])
        elif spec.get("height_ft") is not None:
            p.height_m = float(spec["height_ft"]) * FT
        p.name = spec.get("name") or p.name
        if spec.get("gain_dbi") is not None:
            p.gain_dbi = float(spec["gain_dbi"])
    elif isinstance(spec, (tuple, list)) and len(spec) == 2:
        p = Point(float(spec[0]), float(spec[1]))
    elif isinstance(spec, str):
        s = spec.strip()
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*", s)
        if m:
            p = Point(float(m.group(1)), float(m.group(2)))
        else:
            ll = grid_to_latlon(s)
            if ll is None:
                raise ValueError(f"not a lat,lon or a Maidenhead grid: {spec!r}")
            p = Point(ll[0], ll[1], name=s)
    else:
        raise ValueError(f"not a point: {spec!r}")
    if not (-90 <= p.lat <= 90 and -180 <= p.lon <= 180):
        raise ValueError(f"lat/lon out of range: {p.lat}, {p.lon}")
    if height is not None:
        p.height_m = float(height) * (FT if unit.lower().startswith("f") else 1.0)
    if name:
        p.name = name
    if gain_dbi is not None:
        p.gain_dbi = float(gain_dbi)
    return p


def _path_points(a: Point, b: Point, n: int) -> list[tuple[float, float]]:
    """n points evenly spaced along the great circle from a to b (both included)."""
    def vec(la, lo):
        la, lo = math.radians(la), math.radians(lo)
        return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    va, vb = vec(a.lat, a.lon), vec(b.lat, b.lon)
    ang = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(va, vb)))))
    out = []
    for i in range(n):
        f = i / (n - 1)
        if ang < 1e-12:
            v = va
        else:                                            # spherical linear interpolation
            s1, s2 = math.sin((1 - f) * ang) / math.sin(ang), math.sin(f * ang) / math.sin(ang)
            v = tuple(s1 * x + s2 * y for x, y in zip(va, vb))
        out.append((math.degrees(math.atan2(v[2], math.hypot(v[0], v[1]))), math.degrees(math.atan2(v[1], v[0]))))
    return out


def bearing(a: Point, b: Point) -> float:
    """Initial great-circle bearing a -> b, degrees true (where to point a beam at a)."""
    la1, la2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ------------------------------------------------------------------------------------------------ elevation
#
# Two sources, both free and keyless:
#   usgs        USGS Elevation Point Query Service (3DEP, 1-10 m, US only): one point per query, and slow at times
#               (0.6 s to 30 s a point and some 504s, measured 2026-11-12), so a long uncached path can take minutes.
#   open-meteo  Open-Meteo elevation API (Copernicus GLO-90, 90 m, worldwide): 100 points per query, ~0.2 s. Coarser,
#               which rounds off narrow ridges a little; the sea is 0 m rather than "no data".
# Whichever is chosen is the only one used: no source falls back to another, or to flat ground.

def _usgs_fetch(lat: float, lon: float, timeout: float = 30.0) -> float:
    """One USGS EPQS query -> metres. The service answers JSON {"value": "1620.83", ...}; off its coverage (the sea,
    outside the US) it answers a plain-text "Call failed." or a value of -1000000."""
    req = urllib.request.Request(EPQS_URL.format(lat=lat, lon=lon), headers={"User-Agent": "ham-net-speech-to-text"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    if not body.strip():                                 # seen 2026-11-12 under load: a 200 with nothing in it
        raise ElevationError("USGS elevation service unreachable (an empty answer: overloaded? try again later "
                             "or use source='open-meteo')")
    try:
        v = json.loads(body).get("value")
    except (ValueError, AttributeError):
        raise ElevationError(f"no elevation data at {lat:.4f},{lon:.4f} (USGS: {body.strip()[:80]!r}; "
                             "the service covers the US only, not the open sea)")
    try:
        m = float(v)
    except (TypeError, ValueError):
        raise ElevationError(f"no elevation data at {lat:.4f},{lon:.4f} (USGS value {v!r})")
    if m < -1000:
        raise ElevationError(f"no elevation data at {lat:.4f},{lon:.4f} (USGS no-data value {m:g})")
    return m


def _open_meteo_fetch(pts: list[tuple[float, float]], timeout: float = 30.0) -> list[float]:
    """Up to 100 points in one Open-Meteo query -> metres each."""
    lat = ",".join(f"{p[0]:.5f}" for p in pts)
    lon = ",".join(f"{p[1]:.5f}" for p in pts)
    req = urllib.request.Request(OPEN_METEO_URL.format(lat=lat, lon=lon),
                                 headers={"User-Agent": "ham-net-speech-to-text"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    try:
        got = [float(v) for v in json.loads(body)["elevation"]]
    except (ValueError, KeyError, TypeError):
        raise ElevationError(f"Open-Meteo answered no elevations: {body.strip()[:120]!r}")
    if len(got) != len(pts):
        raise ElevationError(f"Open-Meteo answered {len(got)} elevations for {len(pts)} points")
    return got


SOURCES = {"usgs": "USGS 3DEP (EPQS)", "open-meteo": "Copernicus GLO-90 (Open-Meteo)"}
_cache_lock = threading.Lock()


def _key(lat: float, lon: float) -> tuple[int, int]:
    """Cache key: lat/lon to 4 decimals (~11 m), far finer than the 100 m samples."""
    return (round(lat * 1e4), round(lon * 1e4))


def elevations(pts: list[tuple[float, float]], source: str = "usgs", cache_path: Path | None = DEFAULT_CACHE,
               workers: int = 4, timeout: float = 30.0, retries: int = 2, fetch: Callable | None = None) -> list[float]:
    """Ground elevation (m) at each (lat, lon), from the on-disk cache or `source`, a few queries at a time.
    `fetch` replaces the network call (tests): fetch(lat, lon, timeout) for usgs, fetch(points, timeout) for
    open-meteo. Raises ElevationError when any point cannot be had; the points that did arrive are cached, so a
    second try only asks for the rest. Once one point has failed every retry, no new ones are started."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    name = "USGS" if source == "usgs" else "Open-Meteo"
    if source == "usgs":
        one_fetch = fetch or _usgs_fetch
        batch, batch_fetch = 1, (lambda b, t: [one_fetch(b[0][0], b[0][1], t)])
    else:
        # Open-Meteo counts every 10 points as a call (600 a minute free): one query at a time.
        batch, batch_fetch, workers = 100, (fetch or _open_meteo_fetch), 1
    keys = [_key(la, lo) for la, lo in pts]
    known: dict[tuple[int, int], float] = {}
    con = None
    if cache_path is not None:
        try:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(cache_path, timeout=30)
            con.execute("CREATE TABLE IF NOT EXISTS elevation (src TEXT, lat INTEGER, lon INTEGER, m REAL, "
                        "PRIMARY KEY (src, lat, lon))")
            for k in set(keys):
                row = con.execute("SELECT m FROM elevation WHERE src=? AND lat=? AND lon=?", (source, *k)).fetchone()
                if row:
                    known[k] = row[0]
        except sqlite3.Error:
            con = None                                   # an unusable cache only costs time, never the answer
    missing = sorted(set(keys) - set(known))
    chunks = [missing[i:i + batch] for i in range(0, len(missing), batch)]
    failed: list[ElevationError] = []

    def run(chunk):
        last = None
        for attempt in range(retries + 1):
            if failed:                                   # another point already failed for good: stop asking
                return []
            try:
                return list(zip(chunk, batch_fetch([(k[0] / 1e4, k[1] / 1e4) for k in chunk], timeout)))
            except ElevationError as e:
                last = e                                 # "no data" can be a hiccup too: retry, then report
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
                busy = isinstance(e, urllib.error.HTTPError) and e.code == 429
                last = ElevationError(f"{name} elevation service " + ("refused: too many requests, try again in a "
                                      "minute" if busy else f"unreachable ({e.__class__.__name__}: {e})"))
                if busy and attempt < retries:
                    time.sleep(5.0 * (attempt + 1))     # rate-limited: a longer pause than for a hiccup
                    continue
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
        failed.append(last)
        return []

    try:
        if chunks:
            got = []
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                for part in ex.map(run, chunks):
                    got.extend(part)
            known.update(got)
            if con is not None and got:
                with _cache_lock:
                    try:
                        con.executemany("INSERT OR REPLACE INTO elevation VALUES (?, ?, ?, ?)",
                                        [(source, k[0], k[1], m) for k, m in got])
                        con.commit()
                    except sqlite3.Error:
                        pass
    finally:
        if con is not None:
            con.close()
    if failed:
        raise failed[0]
    return [known[k] for k in keys]


def usgs_elevations(pts, **kw) -> list[float]:
    """elevations() from USGS."""
    return elevations(pts, source="usgs", **kw)


# ------------------------------------------------------------------------------------------------- physics

def fspl_db(d_km: float, mhz: float) -> float:
    """Free-space path loss, dB (d in km, f in MHz)."""
    return 32.44 + 20 * math.log10(d_km) + 20 * math.log10(mhz)


def fresnel_m(d1_m: float, d2_m: float, mhz: float) -> float:
    """Radius of the first Fresnel zone (m) at d1 from one end and d2 from the other."""
    lam = C_M_S / (mhz * 1e6)
    return math.sqrt(lam * d1_m * d2_m / (d1_m + d2_m)) if d1_m > 0 and d2_m > 0 else 0.0


def knife_edge_db(v: float) -> float:
    """ITU-R P.526 single knife-edge diffraction loss J(v), dB; 0 below v = -0.78 (the edge clear of the zone)."""
    if v <= -0.78:
        return 0.0
    return 6.9 + 20 * math.log10(math.sqrt((v - 0.1) ** 2 + 1) + v - 0.1)


def _worst_edge(x, h, i, j, hi, hj, lam):
    """The sample between i and j with the largest Fresnel-Kirchhoff v against the ray from height hi (at x[i]) to
    hj (at x[j]); -> (v, index, metres above the ray) or None when there is nothing between them."""
    best = None
    for k in range(i + 1, j):
        d1, d2 = x[k] - x[i], x[j] - x[k]
        if d1 <= 0 or d2 <= 0:
            continue
        above = h[k] - (hi + (hj - hi) * d1 / (d1 + d2))
        v = above * math.sqrt(2 / lam * (1 / d1 + 1 / d2))
        if best is None or v > best[0]:
            best = (v, k, above)
    return best


def _deygout(x, h, ha, hb, lam, max_edges=3):
    """Deygout's method: the main edge over the whole path, then the worst edge on each side of it measured from
    the main edge's top. One edge = plain P.526 knife edge. -> [(index, metres above its ray, v, loss dB)].
    A side edge counts only when it really blocks its half of the path (v > 0): on smooth or rolling ground every
    sample is a near-grazing "edge", and adding their partial losses would count the same ground two or three
    times (Deygout's known pessimism; 10 km of flat ground at 146 MHz came out 17 dB instead of ~5)."""
    n = len(x)
    main = _worst_edge(x, h, 0, n - 1, ha, hb, lam)
    if main is None or main[0] <= -0.78:
        return []
    v, k, above = main
    edges = [(k, above, v, knife_edge_db(v))]
    if max_edges > 1:
        for sub in (_worst_edge(x, h, 0, k, ha, h[k], lam), _worst_edge(x, h, k, n - 1, h[k], hb, lam)):
            if sub and sub[0] > 0 and len(edges) < max_edges:
                edges.append((sub[1], sub[2], sub[0], knife_edge_db(sub[0])))
    return sorted(edges, key=lambda e: e[0])


def _profile(a: Point, b: Point, step_m: float, max_samples: int, elevation_fn, cache_path, workers, source):
    """(distances from A in m, ground elevations in m) along the path."""
    d_m = km((a.lat, a.lon), (b.lat, b.lon)) * 1000
    if d_m < 1:
        raise ValueError("the two points are the same place")
    n = int(min(max_samples, max(3, math.ceil(d_m / step_m) + 1)))
    pts = _path_points(a, b, n)
    if elevation_fn is None:
        elev = elevations(pts, source=source, cache_path=cache_path, workers=workers)
    else:
        elev = [float(elevation_fn(la, lo)) for la, lo in pts]
    return [d_m * i / (n - 1) for i in range(n)], elev


def _geometry(x, elev, ha, hb, k, mhz):
    """Per sample: earth bulge, effective terrain, ray height, clearance, Fresnel radius (all m)."""
    D = x[-1]
    re_m = k * EARTH_R_M
    bulge = [xi * (D - xi) / (2 * re_m) for xi in x]
    terr = [e + b for e, b in zip(elev, bulge)]
    top_a, top_b = elev[0] + ha, elev[-1] + hb
    ray = [top_a + (top_b - top_a) * xi / D for xi in x]
    clear = [r - t for r, t in zip(ray, terr)]
    fz = [fresnel_m(xi, D - xi, mhz) for xi in x]
    return bulge, terr, top_a, top_b, ray, clear, fz


# ------------------------------------------------------------------------------------------------ analysis

def analyze(a, b, mhz: float = 146.0, *, ha: float | None = None, hb: float | None = None, k: float = 4 / 3,
            watts: float = 50.0, cable_a_db: float = 1.5, cable_b_db: float = 1.5, gain_a_dbi: float = 2.15,
            gain_b_dbi: float = 2.15, sens_voice_dbm: float = SENS_VOICE_DBM, sens_vara_dbm: float = SENS_VARA_DBM,
            sens_packet_dbm: float = SENS_PACKET_DBM, fade_margin_db: float = 10.0, clutter_db: float = 0.0,
            step_m: float = 100.0, max_samples: int = 600, max_edges: int = 3, profile: bool = False,
            elevation_fn: Callable[[float, float], float] | None = None, cache_path: Path | None = DEFAULT_CACHE,
            workers: int = 4, source: str = "usgs", _terrain=None) -> dict:
    """The path from a to b (anything point() accepts; ha/hb = antenna heights in m, overriding the points') at
    `mhz`: terrain clearance, losses, link budget and a plain-words verdict, as a JSON-ready dict ('text' holds the
    same as a few lines for a model or a person). source: "usgs" or "open-meteo" (see elevations());
    elevation_fn(lat, lon) -> m replaces both (tests, other terrain data).
    profile=True adds the terrain/ray/Fresnel rows for a chart (at most ~200)."""
    a, b = point(a), point(b)
    if ha is not None:
        a.height_m = float(ha)
    if hb is not None:
        b.height_m = float(hb)
    if mhz <= 0:
        raise ValueError("mhz must be positive")
    x, elev = _terrain or _profile(a, b, step_m, max_samples, elevation_fn, cache_path, workers, source)
    terrain_src = "the given elevation_fn" if elevation_fn else SOURCES[source]
    D = x[-1]
    lam = C_M_S / (mhz * 1e6)
    bulge, terr, top_a, top_b, ray, clear, fz = _geometry(x, elev, a.height_m, b.height_m, k, mhz)

    # The worst point for clearance: the smallest share of the first Fresnel zone left clear.
    inner = range(1, len(x) - 1)
    ratios = {i: clear[i] / fz[i] for i in inner if fz[i] > 0}
    wi = min(ratios, key=ratios.get)
    min_ratio = ratios[wi]
    min_clear = min(clear[i] for i in inner)
    los = min_clear >= 0
    # Would the path clear if the earth were flat? Then it is the curve of the earth (the radio horizon) that blocks.
    flat_clear = min(ray[i] - elev[i] for i in inner) >= 0

    edges = _deygout(x, terr, top_a, top_b, lam, max_edges=max_edges)
    diff_db = sum(e[3] for e in edges)
    free_db = fspl_db(D / 1000, mhz)
    loss_db = free_db + diff_db + clutter_db

    ga = a.gain_dbi if a.gain_dbi is not None else gain_a_dbi
    gb = b.gain_dbi if b.gain_dbi is not None else gain_b_dbi
    tx_dbm = 10 * math.log10(watts * 1000)
    eirp = tx_dbm - cable_a_db + ga
    rx_dbm = eirp - loss_db + gb - cable_b_db
    sens = {"voice": sens_voice_dbm, "vara_fm": sens_vara_dbm, "packet": sens_packet_dbm}
    margins = {m: rx_dbm - s for m, s in sens.items()}

    def rating(mg):
        return "reliable" if mg >= fade_margin_db else ("marginal" if mg >= 0 else "not expected")

    # Verdict in plain words.
    if min_ratio >= 0.6:
        terrain_words = f"clear line of sight, {min_ratio:.0%} Fresnel clearance"
        kind = "clear"
    elif los:
        tag = "grazing" if min_ratio < 0.25 else "partly in the Fresnel zone"
        terrain_words = (f"line of sight but {tag}: {max(0.0, min_ratio):.0%} Fresnel clearance "
                         f"{x[wi] / 1000:.1f} km from {a.label}")
        if diff_db >= 0.5:
            terrain_words += f": expect ~{diff_db:.0f} dB extra loss"
        kind = "grazing" if min_ratio < 0.25 else "partial"
    else:
        main = max(edges, key=lambda e: e[2]) if edges else (wi, -clear[wi], 0, 0)
        cause = "the curve of the earth (beyond the radio horizon)" if flat_clear else "terrain"
        terrain_words = (f"obstructed by {cause} {x[main[0]] / 1000:.1f} km from {a.label}, "
                         f"{main[1]:.0f} m above the path: expect ~{diff_db:.0f} dB diffraction loss")
        if len(edges) > 1:
            terrain_words += f" ({len(edges)} ridges)"
        kind = "obstructed"
    supports = {m: rating(margins[m]) for m, _ in MODES}
    mode_words = ", ".join(f"{label} {margins[m]:+.0f} dB ({supports[m]})" for m, label in MODES)
    verdict = f"{terrain_words}. Received ~{rx_dbm:.0f} dBm; margin {mode_words}."

    r1 = lambda v: round(v, 1)
    out = {
        "a": {"name": a.label, "lat": round(a.lat, 5), "lon": round(a.lon, 5), "ground_m": r1(elev[0]),
              "antenna_m": r1(a.height_m), "gain_dbi": round(ga, 2)},
        "b": {"name": b.label, "lat": round(b.lat, 5), "lon": round(b.lon, 5), "ground_m": r1(elev[-1]),
              "antenna_m": r1(b.height_m), "gain_dbi": round(gb, 2)},
        "distance_km": round(D / 1000, 2), "mhz": mhz, "k": round(k, 3),
        "bearing_a_to_b_deg": round(bearing(a, b)), "bearing_b_to_a_deg": round(bearing(b, a)),
        "samples": len(x), "sample_spacing_m": round(D / (len(x) - 1)),
        "kind": kind, "line_of_sight": los,
        "min_clearance_m": r1(min_clear), "min_fresnel_clearance": round(min_ratio, 2),
        "worst_point": {"km_from_a": round(x[wi] / 1000, 2), "ground_m": r1(elev[wi]),
                        "above_path_m": r1(-clear[wi]), "fresnel_radius_m": r1(fz[wi])},
        "obstructions": [{"km_from_a": round(x[i] / 1000, 2), "ground_m": r1(elev[i]), "above_path_m": r1(h),
                          "v": round(v, 2), "loss_db": r1(l)} for i, h, v, l in edges],
        "fspl_db": r1(free_db), "diffraction_db": r1(diff_db), "clutter_db": r1(clutter_db),
        "path_loss_db": r1(loss_db),
        "link": {"tx_w": watts, "tx_dbm": r1(tx_dbm), "cable_a_db": cable_a_db, "cable_b_db": cable_b_db,
                 "eirp_dbm": r1(eirp), "rx_dbm": r1(rx_dbm),
                 "sensitivity_dbm": sens, "margin_db": {m: r1(v) for m, v in margins.items()},
                 "fade_margin_wanted_db": fade_margin_db},
        "supports": supports,
        "verdict": verdict,
        "terrain_source": terrain_src,
        "assumptions": (f"terrain from {terrain_src}, k={k:.2f}, ITU-R P.526 "
                        "knife-edge/Deygout diffraction; no buildings, trees or indoor loss (they can add 5-20 dB; "
                        "see clutter_db); both antennas aimed at each other; same figures both directions"),
    }
    if profile:
        every = max(1, math.ceil(len(x) / 200))
        idx = list(range(0, len(x), every)) + ([len(x) - 1] if (len(x) - 1) % every else [])
        out["profile"] = {"columns": ["km_from_a", "ground_m", "earth_bulge_m", "ray_m", "fresnel_radius_m"],
                          "rows": [[round(x[i] / 1000, 3), r1(elev[i]), r1(bulge[i]), r1(ray[i]), r1(fz[i])]
                                   for i in idx]}
    out["text"] = text(out)
    return out


def text(r: dict) -> str:
    """A few plain lines from analyze()'s dict: what a model reads back or a person sees."""
    a, b, ln = r["a"], r["b"], r["link"]
    lines = [
        f"{a['name']} (ground {a['ground_m']:.0f} m, antenna {a['antenna_m']:g} m) -> {b['name']} "
        f"(ground {b['ground_m']:.0f} m, antenna {b['antenna_m']:g} m): {r['distance_km']:.1f} km at "
        f"{r['mhz']:g} MHz, bearing {r['bearing_a_to_b_deg']} deg (back {r['bearing_b_to_a_deg']} deg).",
        f"Terrain: {r['verdict'].split('. Received')[0]}.",
        f"Loss: free space {r['fspl_db']:.1f} dB + diffraction {r['diffraction_db']:.1f} dB"
        + (f" + clutter {r['clutter_db']:.1f} dB" if r["clutter_db"] else "") + f" = {r['path_loss_db']:.1f} dB.",
        f"Signal: {ln['tx_w']:g} W ({ln['tx_dbm']:.1f} dBm), cables -{ln['cable_a_db']:g}/-{ln['cable_b_db']:g} dB, "
        f"antennas {a['gain_dbi']:g}/{b['gain_dbi']:g} dBi -> {ln['rx_dbm']:.1f} dBm received.",
        "Margin: " + ", ".join(f"{label} {ln['margin_db'][m]:+.0f} dB ({r['supports'][m]})" for m, label in MODES)
        + f" (wanted {ln['fade_margin_wanted_db']:g} dB for reliable).",
        f"Assumes {r['assumptions']}.",
    ]
    return "\n".join(lines)


def height_needed(a, b, mhz: float = 146.0, at: str = "a", target: float = 0.6, max_m: float = 30.0, *,
                  ha: float | None = None, hb: float | None = None, k: float = 4 / 3, step_m: float = 100.0,
                  max_samples: int = 600, elevation_fn=None, cache_path: Path | None = DEFAULT_CACHE,
                  workers: int = 4, source: str = "usgs") -> float | None:
    """The lowest antenna height (m above ground, rounded up to 0.1 m) at `at` ("a" or "b") that leaves `target` of
    the first Fresnel zone clear along the whole path, the other end's height unchanged; None if it would take
    more than max_m. 0 means the path is already that clear with the antenna on the ground."""
    a, b = point(a), point(b)
    if ha is not None:
        a.height_m = float(ha)
    if hb is not None:
        b.height_m = float(hb)
    x, elev = _profile(a, b, step_m, max_samples, elevation_fn, cache_path, workers, source)
    _, _, _, _, _, clear, fz = _geometry(x, elev, a.height_m, b.height_m, k, mhz)
    D = x[-1]
    h0 = a.height_m if at == "a" else b.height_m
    need = 0.0
    # Raising one end lifts the ray at each sample by (1 - x/D) of the rise (end A) or x/D (end B).
    for i in range(1, len(x) - 1):
        w = (1 - x[i] / D) if at == "a" else (x[i] / D)
        if w > 0 and fz[i] > 0:
            need = max(need, h0 + (target * fz[i] - clear[i]) / w)
    need = math.ceil(max(0.0, need) * 10 - 1e-9) / 10
    return None if need > max_m else need


# ---------------------------------------------------------------------------------------------- networks

def _short(r: dict) -> str:
    return {"clear": "clear", "grazing": "grazing", "partial": "partial Fresnel",
            "obstructed": f"obstructed ({r['diffraction_db']:.0f} dB)"}[r["kind"]]


def matrix(points: list, mhz: float = 146.0, **kw) -> dict:
    """Every pair of sites: verdict and margins, to see which sites reach which directly. kw go to analyze()
    (gain_a_dbi/gain_b_dbi apply to every site unless a Point carries its own gain_dbi)."""
    pts = [point(p) for p in points]
    rows = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            r = analyze(pts[i], pts[j], mhz, **kw)
            rows.append({"a": pts[i].label, "b": pts[j].label, "km": r["distance_km"], "kind": r["kind"],
                         "terrain": _short(r), "fresnel_clearance": r["min_fresnel_clearance"],
                         "path_loss_db": r["path_loss_db"], "rx_dbm": r["link"]["rx_dbm"],
                         "margin_db": r["link"]["margin_db"], "supports": r["supports"]})
    lines = [f"Every pair at {mhz:g} MHz (margin over voice / VARA FM / packet sensitivity):"]
    for x in rows:
        m = x["margin_db"]
        lines.append(f"  {x['a']} <-> {x['b']}: {x['km']:.1f} km, {x['terrain']}, {x['rx_dbm']:.0f} dBm, "
                     f"margin {m['voice']:+.0f} / {m['vara_fm']:+.0f} / {m['packet']:+.0f} dB")
    return {"mhz": mhz, "pairs": rows, "text": "\n".join(lines)}


def best_relay(points: list, candidates: list, mhz: float = 146.0, mode: str = "vara_fm",
               min_margin_db: float | None = None, **kw) -> dict:
    """Which candidate site (a hilltop, a repeater or digipeater location, a tall building) links the most sites
    with at least min_margin_db (default analyze()'s fade_margin_db, 10 dB) for `mode` (voice, vara_fm, packet).
    Ranked by sites reached, then by the weakest margin among all sites. kw go to analyze()."""
    if mode not in dict(MODES):
        raise ValueError(f"mode must be one of {', '.join(dict(MODES))}")
    want = min_margin_db if min_margin_db is not None else kw.get("fade_margin_db", 10.0)
    pts = [point(p) for p in points]
    ranking = []
    for c in (point(c) for c in candidates):
        sites = []
        for p in pts:
            r = analyze(c, p, mhz, **kw)
            mg = r["link"]["margin_db"][mode]
            sites.append({"name": p.label, "km": r["distance_km"], "terrain": _short(r), "margin_db": mg,
                          "ok": mg >= want})
        reached = sum(s["ok"] for s in sites)
        ranking.append({"candidate": c.label, "antenna_m": c.height_m, "reaches": reached, "of": len(pts),
                        "weakest_margin_db": min(s["margin_db"] for s in sites), "sites": sites})
    ranking.sort(key=lambda r: (-r["reaches"], -r["weakest_margin_db"]))
    label = dict(MODES)[mode]
    lines = [f"Relay candidates at {mhz:g} MHz for {label} (wanted margin {want:g} dB):"]
    for r in ranking:
        missed = [s["name"] for s in r["sites"] if not s["ok"]]
        lines.append(f"  {r['candidate']}: reaches {r['reaches']} of {r['of']}, weakest margin "
                     f"{r['weakest_margin_db']:+.0f} dB" + (f"; not: {', '.join(missed)}" if missed else ""))
    best = ranking[0]["candidate"] if ranking and ranking[0]["reaches"] else None
    return {"mhz": mhz, "mode": mode, "wanted_margin_db": want, "best": best, "ranking": ranking,
            "text": "\n".join(lines)}


# --------------------------------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hamkit.pathprofile",
                                 description="VHF/UHF path over real terrain: clearance, loss, link margin.")
    ap.add_argument("a", help="site A: lat,lon or a Maidenhead grid")
    ap.add_argument("b", help="site B: lat,lon or a Maidenhead grid")
    ap.add_argument("--mhz", type=float, default=146.0)
    ap.add_argument("--ha", type=float, default=10.0, help="antenna height above ground at A (m; --ft for feet)")
    ap.add_argument("--hb", type=float, default=6.0, help="antenna height above ground at B")
    ap.add_argument("--ft", action="store_true", help="--ha/--hb are in feet")
    ap.add_argument("--watts", type=float, default=50.0)
    ap.add_argument("--cable-db", type=float, default=1.5, help="cable loss at each end, dB")
    ap.add_argument("--gain-a", type=float, default=2.15, help="antenna gain at A, dBi")
    ap.add_argument("--gain-b", type=float, default=2.15, help="antenna gain at B, dBi")
    ap.add_argument("--clutter-db", type=float, default=0.0, help="allowance for buildings/trees, dB")
    ap.add_argument("--k", type=float, default=4 / 3, help="effective earth radius factor")
    ap.add_argument("--step", type=float, default=100.0, help="terrain sample spacing, m")
    ap.add_argument("--source", choices=sorted(SOURCES), default="usgs",
                    help="terrain: usgs (3DEP, US, one point a query) or open-meteo (90 m, worldwide, fast)")
    ap.add_argument("--name-a")
    ap.add_argument("--name-b")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--profile", action="store_true", help="with --json: include the terrain rows")
    o = ap.parse_args(argv)
    unit = "ft" if o.ft else "m"
    try:
        r = analyze(point(o.a, o.ha, unit, o.name_a or "A"), point(o.b, o.hb, unit, o.name_b or "B"), o.mhz,
                    k=o.k, watts=o.watts, cable_a_db=o.cable_db, cable_b_db=o.cable_db, gain_a_dbi=o.gain_a,
                    gain_b_dbi=o.gain_b, clutter_db=o.clutter_db, step_m=o.step, profile=o.profile,
                    source=o.source)
    except (ElevationError, ValueError) as e:
        print(f"pathprofile: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2) if o.json else r["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
