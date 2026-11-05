"""Set level…: judging the noise and voice levels, the automatic Windows-level loop, and the dashboard route."""
import json
import threading
import urllib.error
import urllib.request

import numpy as np

from station import levels
from station.server import serve
from tests.test_station import station  # noqa: F401 — the fixture


def readings(rms, peak, n=20, rx=False, t0=100.0):
    return [(t0 + i * 0.1, rms, peak, rx) for i in range(n)]


def test_band_by_mode_and_gate():
    assert levels.band_for("FM", "voice") == levels.NOISE_FM
    assert levels.band_for("USB", "carrier") == levels.NOISE_FM          # a data jack's hiss
    assert levels.band_for("LSB", "voice") == levels.NOISE_SSB


def test_stats_window_and_voice_only_while_receiving():
    r = readings(-40, -30, rx=False) + readings(-20, -8, rx=True, t0=102.0)
    assert levels.stats(r, since=102.0)["rms"] == -20
    assert levels.stats(r, voice=True)["peak"] == -8 and levels.stats(r, voice=True)["n"] == 20
    assert levels.stats([], None) == {"n": 0}


def test_noise_advice_matches_the_laptop_measurements():
    """2026-11-05, a handheld's speaker output into a Digirig, squelch open: -13 (peaks -0.7), -42, then -24."""
    fm = levels.NOISE_FM
    hot = levels.advise("noise", levels.stats(readings(-13, -0.7)), fm)
    assert hot["state"] == "clip" and hot["change_db"] == -12 and "down about 12 dB" in hot["say"]
    low = levels.advise("noise", levels.stats(readings(-42, -29)), fm)
    assert low["state"] == "low" and low["change_db"] == 17 and "up about 17 dB" in low["say"]
    assert levels.advise("noise", levels.stats(readings(-24, -11)), fm)["state"] == "ok"
    assert levels.advise("noise", levels.stats(readings(-72, -60)), fm)["state"] == "dead"
    assert levels.advise("noise", levels.stats(readings(-24, -11, n=3)), fm)["state"] == "none"


def test_voice_advice():
    v = lambda peak: levels.advise("voice", levels.stats(readings(-20, peak, rx=True), voice=True), levels.NOISE_FM)
    assert v(-0.5)["state"] == "clip" and v(-2)["state"] == "high" and v(-8)["state"] == "ok" and v(-20)["state"] == "low"
    assert levels.advise("voice", levels.stats(readings(-20, -8, rx=False), voice=True), levels.NOISE_FM)["state"] == "none"


class FakeGain:
    """Windows' recording level; the measured hiss follows it dB for dB."""
    def __init__(self, db, lo=0.0, hi=23.8):
        self.db, self.lo, self.hi = db, lo, hi

    def get(self):
        return {"db": self.db, "min": self.lo, "max": self.hi, "pct": 0, "muted": False}

    def set_db(self, db):
        self.db = max(self.lo, min(self.hi, db))
        return self.get()


def run_auto(gain, radio_db):
    clock = [0.0]
    measure = lambda since: levels.stats(readings(radio_db + gain.db, radio_db + gain.db + 13, t0=clock[0]))
    return levels.auto(measure, gain, levels.NOISE_FM, sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                       clock=lambda: clock[0])


def test_auto_brings_the_noise_into_the_band():
    g = FakeGain(0.7)                                   # the 6 % the laptop was left at: -42 dBFS
    res = run_auto(g, -42.7)
    assert res["ok"] and -30 <= -42.7 + g.db <= -20 and len(res["steps"]) == 2


def test_auto_at_the_end_of_the_windows_range_asks_for_the_radio():
    g = FakeGain(0.0)                                   # far too loud even at the minimum: the radio must go down
    res = run_auto(g, -5.0)
    assert not res["ok"] and "Windows is at its minimum: turn the radio's volume down" in res["advice"]["say"]
    g = FakeGain(23.8)
    res = run_auto(g, -70.0)
    assert not res["ok"] and "maximum" in res["advice"]["say"]


def test_level_route(station):  # noqa: F811
    srv = serve(station, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        import time
        now = time.time()
        station.level_recent["ftm"].extend((now - 2 + i * 0.1, -24.0, -11.0, False) for i in range(15))
        d = json.loads(urllib.request.urlopen(base + "/api/radios/ftm/level").read())
        assert d["band"] == list(levels.NOISE_FM) and d["advice"]["state"] == "ok" and d["gain"] is None
        assert json.loads(urllib.request.urlopen(base + "/api/radios/ftm/level?step=voice").read())["band"] == \
            list(levels.VOICE_PEAK)
        req = urllib.request.Request(base + "/api/radios/ftm/level", json.dumps({"action": "auto"}).encode(),
                                     {"Content-Type": "application/json"})
        try:                                            # no Windows level here (and the test radio is a file)
            urllib.request.urlopen(req)
            raise AssertionError("expected 409")
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())["error"]
            assert e.code == 409 and ("can't be set" in err or "not listening" in err)
        try:
            urllib.request.urlopen(base + "/api/radios/nope/level")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_crest_tells_clean_hiss_from_a_clipping_amplifier():
    rng = np.random.default_rng(1)
    x = rng.standard_normal(16000 * 3) * 0.05
    clean = levels.analyze(x)
    assert 10 <= clean["crest"] <= 11.5 and abs(clean["rms"] - 20 * np.log10(0.05)) < 0.5
    clipped = levels.analyze(np.clip(x, -0.06, 0.06))
    assert clipped["crest"] < levels.CREST_MIN
    t = np.arange(16000 * 3) / 16000
    assert levels.analyze(0.05 * np.sin(2 * np.pi * 60 * t) + x * 0.1)["hum"] > -3


def pt(knob, o_rms, c_rms, crest=10.5, o_gain=10.0, c_gain=10.0):
    return {"knob": knob, "open": {"rms": o_rms, "peak": o_rms + 12, "crest": crest, "hum": -40},
            "closed": {"rms": c_rms, "peak": c_rms + 12, "crest": 10, "hum": -40},
            "open_gain_db": o_gain, "closed_gain_db": c_gain}


def test_margin_compensates_for_a_windows_level_change_between_open_and_closed():
    assert levels.knob_point(pt("1/4", -25, -60))["margin"] == 35
    assert levels.knob_point(pt("1/4", -25, -55, o_gain=10, c_gain=15))["margin"] == 35


def test_best_knob_is_the_lowest_clean_one_near_the_best_margin():
    res = levels.best_knob([pt("1/2", -25, -64), pt("1/4", -25, -56), pt("3/8", -25, -63), pt("3/4", -25, -70, crest=6)])
    assert res["best"] == "3/8" and "3/8" in res["say"]               # 3/4 distorts; 1/2 is only 1 dB better
    assert levels.best_knob([pt("3/4", -25, -70, crest=6)])["best"] is None
    assert levels.best_knob([{"knob": "1/4", "open": pt("1/4", -25, -60)["open"]}])["best"] is None


def test_knob_route(station):  # noqa: F811
    srv = serve(station, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        station.knob_points["ftm"]["1/4"] = pt("1/4", -25, -60)
        d = json.loads(urllib.request.urlopen(base + "/api/radios/ftm/level?step=knob").read())
        assert d["knobs"] == levels.KNOBS and d["result"]["best"] == "1/4"
        assert station.level("ftm", "knob")["result"]["margin"] == 35
        station.radios["ftm"]["state"] = "listening"
        station._level_ctx = lambda rid: (levels, station.radios[rid], levels.NOISE_FM, FakeGain(5.0))
        assert station.set_level("ftm", {"action": "knob_reset"})["result"]["best"] is None
        station.level_tap.clear()
        threading.Timer(0.2, lambda: station.level_tap["ftm"].extend([np.full(16000, 0.01, np.float32)] * 4)).start()
        out = station.set_level("ftm", {"action": "knob", "knob": "1/2", "squelch": "closed"})
        assert out["result"]["points"][0]["closed"]["rms"] == -40.0
        import pytest
        with pytest.raises(ValueError):
            station.set_level("ftm", {"action": "knob", "knob": "11", "squelch": "open"})
    finally:
        srv.shutdown()
