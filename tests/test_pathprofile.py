"""hamkit/pathprofile.py: VHF/UHF path analysis, offline (synthetic terrain; a fake USGS for the cache/error paths)."""
import json
import math
import urllib.error

import pytest

from hamkit import pathprofile as pp
from hamkit.zipgeo import km

# Invented sites on an east-west line near 40 N (0.01 deg of longitude ~ 0.852 km there).
LAT = 40.0
A = pp.Point(LAT, -105.30, 10, "EOC")
B10 = pp.Point(LAT, -105.30 + 10 / 85.2, 6, "Shelter")          # ~10 km east of A
OFF = dict(cache_path=None)


def flat(lat, lon):
    return 1600.0


def valley(lat, lon):
    """Ground at 1600 m at A and B (10 km apart) falling to 1300 m between them: a path that clears easily."""
    f = min(1.0, max(0.0, (lon + 105.30) / (10 / 85.2)))
    return 1600.0 - 300.0 * math.sin(math.pi * f)


def ridge_at(lon0, height, width_deg=0.004):
    """Flat ground at 1600 m with a ridge `height` m high centred on longitude lon0."""
    def f(lat, lon):
        return 1600.0 + (height if abs(lon - lon0) <= width_deg / 2 else 0.0)
    return f


def test_flat_short_path_is_clear():
    r = pp.analyze(A, B10, 146.0, elevation_fn=valley, **OFF)
    assert r["kind"] == "clear" and r["line_of_sight"]
    assert r["min_fresnel_clearance"] >= 0.6 and r["diffraction_db"] == 0
    assert r["verdict"].startswith("clear line of sight")
    assert r["supports"] == {"voice": "reliable", "vara_fm": "reliable", "packet": "reliable"}
    assert abs(r["distance_km"] - 10) < 0.2 and r["bearing_a_to_b_deg"] == 90
    assert "profile" not in r and json.dumps(r)                       # JSON-ready
    # Flat ground is line of sight, but 10 m and 6 m masts leave the 2 m Fresnel zone (72 m wide mid-path) mostly
    # in the ground: grazing, with a few dB of loss.
    g = pp.analyze(A, B10, 146.0, elevation_fn=flat, **OFF)
    assert g["line_of_sight"] and g["kind"] == "grazing" and 0 < g["diffraction_db"] < 10


def test_ridge_in_the_middle_is_obstructed():
    mid = -105.30 + 4.2 / 85.2                                        # 4.2 km from A
    r = pp.analyze(A, B10, 146.0, elevation_fn=ridge_at(mid, 90), **OFF)
    assert r["kind"] == "obstructed" and not r["line_of_sight"]
    ob = max(r["obstructions"], key=lambda o: o["above_path_m"])
    assert abs(ob["km_from_a"] - 4.2) < 0.3
    assert 75 < ob["above_path_m"] < 90                               # ridge 90 m, ray ~8 m up there, small bulge
    assert r["diffraction_db"] > 10
    assert r["path_loss_db"] == pytest.approx(r["fspl_db"] + r["diffraction_db"], abs=0.11)
    assert "obstructed by terrain 4." in r["verdict"] and "km from EOC" in r["verdict"]
    assert "diffraction" in r["text"]


def test_several_ridges_use_deygout():
    lons = [-105.30 + d / 85.2 for d in (2.5, 5.0, 7.5)]
    def f(lat, lon):
        # a 100 m ridge mid-path with a 75 m ridge each side of it: the main edge, then one each side
        return 1600.0 + sum(h for l0, h in zip(lons, (75, 100, 75)) if abs(lon - l0) <= 0.002)
    one = pp.analyze(A, B10, 146.0, elevation_fn=ridge_at(lons[1], 100), **OFF)
    three = pp.analyze(A, B10, 146.0, elevation_fn=f, **OFF)
    assert [o["km_from_a"] for o in three["obstructions"]] == pytest.approx([2.5, 5.0, 7.5], abs=0.25)
    assert three["diffraction_db"] > one["diffraction_db"] and "(3 ridges)" in three["verdict"]
    assert len(pp.analyze(A, B10, 146.0, elevation_fn=f, max_edges=1, **OFF)["obstructions"]) == 1


def test_earth_curvature_blocks_a_long_low_path():
    far = pp.Point(LAT, -105.30 + 80 / 85.2, 2, "Far")              # 80 km, 2 m masts: horizon ~12 km
    r = pp.analyze(pp.Point(LAT, -105.30, 2, "Near"), far, 146.0, elevation_fn=flat, **OFF)
    assert not r["line_of_sight"] and "curve of the earth" in r["verdict"]
    assert r["diffraction_db"] > 20
    # Infinite k (flat earth) puts it back in line of sight.
    assert pp.analyze(pp.Point(LAT, -105.30, 2), far, 146.0, k=1e9, elevation_fn=flat, **OFF)["line_of_sight"]


def test_fresnel_radius_and_fspl_textbook():
    for d1, d2, f in ((5.0, 5.0, 146.0), (2.0, 8.0, 445.0), (12.0, 3.0, 223.5)):
        book = 17.32 * math.sqrt(d1 * d2 / (f / 1000 * (d1 + d2)))
        assert pp.fresnel_m(d1 * 1000, d2 * 1000, f) == pytest.approx(book, rel=0.002)
    for d, f in ((10.0, 146.0), (1.0, 445.0), (37.0, 223.5)):
        assert pp.fspl_db(d, f) == pytest.approx(32.44 + 20 * math.log10(d) + 20 * math.log10(f))
    r = pp.analyze(A, B10, 445.0, elevation_fn=flat, **OFF)
    assert r["fspl_db"] == pytest.approx(pp.fspl_db(r["distance_km"], 445.0), abs=0.06)
    assert pp.knife_edge_db(0) == pytest.approx(6.0, abs=0.1) and pp.knife_edge_db(-1) == 0


def test_link_budget_and_margins():
    r = pp.analyze(A, B10, 146.0, watts=50, cable_a_db=2, cable_b_db=1, gain_a_dbi=6, gain_b_dbi=0,
                   elevation_fn=valley, **OFF)
    ln = r["link"]
    assert ln["tx_dbm"] == pytest.approx(47.0, abs=0.05)
    assert ln["rx_dbm"] == pytest.approx(47.0 - 2 + 6 - r["path_loss_db"] + 0 - 1, abs=0.15)
    assert ln["margin_db"]["voice"] == pytest.approx(ln["rx_dbm"] + 120, abs=0.1)
    assert ln["margin_db"]["vara_fm"] == pytest.approx(ln["rx_dbm"] + 110, abs=0.1)
    # A weak link: margin below zero for data but some for voice.
    w = pp.analyze(A, B10, 146.0, watts=0.001, clutter_db=20, elevation_fn=valley, **OFF)   # rx ~ -114 dBm
    assert w["supports"] == {"voice": "marginal", "vara_fm": "not expected", "packet": "not expected"}
    assert "clutter 20.0 dB" in w["text"]


def test_points_grid_feet_and_dict():
    p = pp.point("40.015, -105.27", height=33, unit="ft", name="Club")
    assert (p.lat, p.lon, round(p.height_m, 2), p.name) == (40.015, -105.27, 10.06, "Club")
    g = pp.point("DN70la")
    assert abs(g.lat - 40.02) < 0.05 and abs(g.lon + 105.04) < 0.05 and g.name == "DN70la"
    d = pp.point({"lat": 40, "lon": -105, "height_ft": 20, "name": "Hill", "gain_dbi": 5})
    assert round(d.height_m, 2) == 6.1 and d.gain_dbi == 5
    with pytest.raises(ValueError):
        pp.point("not a place")
    with pytest.raises(ValueError):
        pp.analyze(A, A, elevation_fn=flat, **OFF)


def test_profile_rows_for_a_chart():
    r = pp.analyze(A, B10, elevation_fn=flat, profile=True, **OFF)
    rows = r["profile"]["rows"]
    assert len(rows) <= 202 and rows[0][0] == 0 and rows[-1][0] == pytest.approx(r["distance_km"], abs=0.01)
    assert r["samples"] == 101 and r["sample_spacing_m"] == pytest.approx(100, abs=2)
    long = pp.analyze(A, pp.Point(LAT, -105.30 + 90 / 85.2), elevation_fn=flat, **OFF)
    assert long["samples"] == 600


def test_height_needed_reaches_60_percent():
    near = -105.30 + 2 / 85.2                                       # a ridge 2 km from A, 12 m under the ray
    def terrain(lat, lon):
        return 1588.0 if abs(lon - near) <= 0.002 else valley(lat, lon)
    low = pp.analyze(A, B10, elevation_fn=terrain, **OFF)
    assert low["min_fresnel_clearance"] < 0.6
    h = pp.height_needed(A, B10, at="a", elevation_fn=terrain, **OFF)
    assert h is not None and h > A.height_m
    assert pp.analyze(A, B10, ha=h, elevation_fn=terrain, **OFF)["min_fresnel_clearance"] >= 0.6
    assert pp.analyze(A, B10, ha=h - 0.5, elevation_fn=terrain, **OFF)["min_fresnel_clearance"] < 0.6
    # From the far end a mast lifts the path at the ridge only a fifth as much: more than 30 m.
    assert pp.height_needed(A, B10, at="b", elevation_fn=terrain, **OFF) is None
    assert pp.height_needed(A, B10, at="b", max_m=200, elevation_fn=terrain, **OFF) > 30
    assert pp.height_needed(A, B10, elevation_fn=valley, **OFF) < A.height_m   # already clear


def test_matrix_and_best_relay_pick_the_hilltop():
    # Three sites in a valley with ridges between them, and a 300 m hill in the middle that sees them all.
    hill = pp.Point(LAT, -105.30 + 5 / 85.2, 10, "Hilltop")
    low = pp.Point(LAT - 0.03, -105.30 + 5 / 85.2, 10, "Low spot")
    sites = [pp.Point(LAT, -105.30, 6, "EOC"), pp.Point(LAT, -105.30 + 10 / 85.2, 6, "Church 1"),
             pp.Point(LAT + 0.04, -105.30 + 5 / 85.2, 6, "Church 2")]

    def terrain(lat, lon):
        d = km((lat, lon), (hill.lat, hill.lon))
        h = 1600.0 + (300.0 if d < 0.4 else 0.0)                     # the hill
        for s in sites:                                               # an 80 m ring of ridge 1.5 km round each site
            if 1.3 < km((lat, lon), (s.lat, s.lon)) < 1.7 and d >= 0.4:
                h = max(h, 1680.0)
        return h

    m = pp.matrix(sites, watts=5, elevation_fn=terrain, **OFF)
    assert len(m["pairs"]) == 3 and all(p["kind"] == "obstructed" for p in m["pairs"])
    assert "EOC <-> Church 1" in m["text"]
    br = pp.best_relay(sites, [low, hill], watts=5, elevation_fn=terrain, **OFF)
    assert br["best"] == "Hilltop" and br["ranking"][0]["reaches"] == 3
    assert br["ranking"][1]["candidate"] == "Low spot" and br["ranking"][1]["reaches"] < 3
    assert "Hilltop: reaches 3 of 3" in br["text"]


class FakeUSGS:
    def __init__(self, fail=None):
        self.calls, self.fail = 0, fail

    def __call__(self, lat, lon, timeout):
        self.calls += 1
        if self.fail:
            raise self.fail
        return 1500.0 + lat


def test_cache_hit_avoids_a_second_fetch(tmp_path):
    db = tmp_path / "elevation.sqlite"
    pts = [(40.0, -105.0), (40.001, -105.0), (40.00001, -105.00001)]    # the last rounds onto the first
    f = FakeUSGS()
    got = pp.usgs_elevations(pts, cache_path=db, fetch=f)
    assert f.calls == 2 and got[0] == got[2] == pytest.approx(1540.0)
    g = FakeUSGS()
    assert pp.usgs_elevations(pts, cache_path=db, fetch=g) == got and g.calls == 0


def test_usgs_errors_surface(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.time, "sleep", lambda s: None)
    down = FakeUSGS(urllib.error.URLError("connection refused"))
    with pytest.raises(pp.ElevationError, match="unreachable"):
        pp.usgs_elevations([(40.0, -105.0)], cache_path=tmp_path / "e.sqlite", fetch=down)
    assert down.calls == 3                                            # tried, then two retries
    sea = FakeUSGS(pp.ElevationError("no elevation data at 36.5,-123.5"))
    with pytest.raises(pp.ElevationError, match="no elevation data"):
        pp.usgs_elevations([(36.5, -123.5)], cache_path=None, fetch=sea)
    # analyze() with no elevation_fn goes to USGS and lets the error through (no flat-earth fallback).
    monkeypatch.setattr(pp, "_usgs_fetch", down)
    with pytest.raises(pp.ElevationError):
        pp.analyze(A, B10, cache_path=tmp_path / "e2.sqlite")


def test_usgs_response_parsing(monkeypatch):
    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.body.encode()
    for body, want in (('{"value":"1620.839233398","resolution":1}', 1620.839233398),
                       ('{"value":1234.5}', 1234.5)):
        monkeypatch.setattr(pp.urllib.request, "urlopen", lambda req, timeout, b=body: Resp(b))
        assert pp._usgs_fetch(40.0, -105.0) == pytest.approx(want)
    for body in ("Call failed.  [Failed cloud operation]", '{"value":"-1000000"}', '{"value":null}'):
        monkeypatch.setattr(pp.urllib.request, "urlopen", lambda req, timeout, b=body: Resp(b))
        with pytest.raises(pp.ElevationError, match="no elevation data"):
            pp._usgs_fetch(36.5, -123.5)
    monkeypatch.setattr(pp.urllib.request, "urlopen", lambda req, timeout: Resp(""))
    with pytest.raises(pp.ElevationError, match="unreachable.*open-meteo"):
        pp._usgs_fetch(40.0, -105.0)


def test_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(pp, "_usgs_fetch", lambda lat, lon, timeout: valley(lat, lon))
    monkeypatch.setattr(pp, "DEFAULT_CACHE", None)
    real = pp.analyze
    monkeypatch.setattr(pp, "analyze", lambda *a, **k: real(*a, **{**k, "cache_path": None}))
    assert pp.main(["40.0,-105.30", "40.0,-105.19", "--mhz", "445", "--ha", "33", "--hb", "20", "--ft",
                    "--watts", "25", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["mhz"] == 445 and r["a"]["antenna_m"] == pytest.approx(10.1, abs=0.05) and r["kind"] == "clear"
    assert pp.main(["40.0,-105.30", "40.0,-105.19"]) == 0
    assert "Margin: FM voice" in capsys.readouterr().out


def test_open_meteo_batches_and_caches_apart_from_usgs(tmp_path):
    db = tmp_path / "elevation.sqlite"
    pts = [(40.0 + i / 1000, -105.0) for i in range(250)]
    batches = []
    def om(chunk, timeout):
        batches.append(len(chunk))
        return [1700.0 for _ in chunk]
    assert pp.elevations(pts, source="open-meteo", cache_path=db, fetch=om) == [1700.0] * 250
    assert sorted(batches) == [50, 100, 100]                          # 100 points a query
    u = FakeUSGS()
    assert pp.usgs_elevations(pts[:3], cache_path=db, fetch=u)[0] == pytest.approx(1540.0) and u.calls == 3
    assert pp.elevations(pts, source="open-meteo", cache_path=db, fetch=om)[0] == 1700.0 and len(batches) == 3
    with pytest.raises(ValueError):
        pp.elevations(pts, source="nowhere")


def test_a_failure_keeps_what_arrived_and_stops_asking(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.time, "sleep", lambda s: None)
    db = tmp_path / "elevation.sqlite"
    pts = [(40.0 + i / 1000, -105.0) for i in range(40)]
    calls = []
    def flaky(lat, lon, timeout):
        calls.append(lat)
        if round(lat * 1000) == 40005:
            raise TimeoutError("read timed out")
        return 1600.0
    with pytest.raises(pp.ElevationError, match="unreachable"):
        pp.usgs_elevations(pts, cache_path=db, fetch=flaky, workers=1)
    assert len(calls) == 5 + 3                                        # 5 good, 3 tries at the bad one, then stop
    ok = FakeUSGS()
    pp.usgs_elevations(pts[:5], cache_path=db, fetch=ok)
    assert ok.calls == 0                                              # the 5 that arrived were cached


def test_rate_limited_says_so(monkeypatch):
    slept = []
    monkeypatch.setattr(pp.time, "sleep", slept.append)
    def busy(chunk, timeout):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    with pytest.raises(pp.ElevationError, match="too many requests"):
        pp.elevations([(40.0, -105.0)], source="open-meteo", cache_path=None, fetch=busy)
    assert slept == [5.0, 10.0]
