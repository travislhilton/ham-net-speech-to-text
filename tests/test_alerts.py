"""The pre-net alert (station/alerts.py) and the repeating schedule honoured by the station (listen windows,
current/next net, net names on transmissions), plus the schedule's skip / move / restore / preview API."""
import copy
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from station import engine
from station.core import Station
from station.server import serve

CFG = {"callsign": "W9XYZ",
       "radios": [{"id": "ftm", "name": "FTM-150R", "device": "NET-REC", "gate": "carrier", "freq_mhz": 145.9},
                  {"id": "ic705", "name": "IC-705", "device": "IC-705", "gate": "voice", "freq_mhz": 7.185, "mode": "LSB"}],
       "schedule": [{"id": "sun", "name": "Sunday Net", "radio": "ftm", "freq_mhz": 146.94, "mode": "FM",
                     "start": "19:00", "duration_min": 60, "ncs": "K7NET",
                     "repeat": {"freq": "monthly", "nth": [1, 3], "weekdays": ["Sun"]}}],
       "port": 0, "bind": "127.0.0.1", "qrz_online": False}
SUN = datetime(2026, 11, 15)          # the 3rd Sunday of November 2026


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, t, cfg=CFG):
    clock = Clock(t)
    return Station(copy.deepcopy(cfg), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=clock), clock


def drain(q, kind):
    out = []
    while not q.empty():
        k, d = q.get_nowait()
        if k == kind:
            out.append(d)
    return out


def test_alert_five_minutes_before_with_actions(tmp_path):
    st, clock = make(tmp_path, SUN.replace(hour=18, minute=54))
    q = st.bus.subscribe()
    st.nets.tick()
    assert st.nets.alerts.last == []                                             # 6 minutes before: nothing yet
    clock.t = SUN.replace(hour=18, minute=55)
    st.nets.tick()
    (a,) = drain(q, "alerts")[-1]["alerts"]
    assert a["key"] == "sun@2026-11-15" and a["state"] == "due" and a["phase"] == "soon" and a["minutes"] == 5
    assert a["text"] == "Sunday Net starts at 19:00 on FTM-150R: set it to 146.940 MHz FM"
    assert st.snapshot()["alerts"][0]["key"] == a["key"]                         # a page opened now shows it too
    clock.t = SUN.replace(hour=18, minute=56)
    st.nets.tick()
    assert drain(q, "alerts")[-1]["alerts"][0]["minutes"] == 4                   # the countdown reaches every screen
    # "Remind me in 1 min": gone, then back
    st.nets.alerts.act(a["key"], "snooze")
    assert st.nets.alerts.last == []
    clock.t = SUN.replace(hour=18, minute=57, second=1)
    st.nets.tick()
    assert st.nets.alerts.last[0]["state"] == "due"
    # "Radio is set": logs the radio at the net's frequency and mode, like Change
    st.nets.alerts.act(a["key"], "ready")
    r = st.radios["ftm"]
    assert r["freq_mhz"] == 146.94 and r["mode"] == "FM" and st.nets.alerts.last[0]["state"] == "ready"
    assert json.loads((tmp_path / "operator.json").read_text())["radios"]["ftm"]["freq_mhz"] == 146.94
    clock.t = SUN.replace(hour=19, minute=2)
    st.nets.tick()
    assert st.nets.alerts.last[0]["phase"] == "started"                          # stays up as the net opens
    clock.t = SUN.replace(hour=19, minute=10)
    st.nets.tick()
    assert st.nets.alerts.last == []                                             # gone 10 minutes after the start
    with pytest.raises(LookupError):
        st.nets.alerts.act("sun@2026-11-15", "ready")
    with pytest.raises(ValueError):
        st.nets.alerts.act("sun@2026-11-15", "explode")


def test_already_on_frequency_and_per_net_minutes(tmp_path):
    cfg = copy.deepcopy(CFG)
    cfg["schedule"][0]["alert_min"] = 15
    cfg["schedule"].append({"id": "hf", "name": "HF Net", "radio": "ic705", "start": "19:00", "duration_min": 30,
                            "alert_min": 0, "repeat": {"freq": "daily"}})
    st, clock = make(tmp_path, SUN.replace(hour=18, minute=46), cfg)
    st.set_radio("ftm", 146.94, "FM")
    st.nets.tick()
    (a,) = st.nets.alerts.last                                                   # the HF net has its alert off
    assert a["state"] == "already" and a["minutes"] == 14 and "ready" in a["text"]
    st.nets.alerts.act(a["key"], "dismiss")
    assert st.nets.alerts.last == []


def test_skip_this_net_keeps_the_radio_paused_and_survives_a_restart(tmp_path):
    st, clock = make(tmp_path, SUN.replace(hour=12))
    st.nets.set_listen("ftm", {"scheduled": True})
    assert st.radios["ftm"]["paused"]
    clock.t = SUN.replace(hour=18, minute=56)
    st.nets.tick()
    st.nets.alerts.act("sun@2026-11-15", "skip")
    assert st.nets.alerts.last == []
    again, c2 = make(tmp_path, SUN.replace(hour=18, minute=59, second=30))
    again.nets.tick()
    assert again.radios["ftm"]["paused"] and again.nets.alerts.last == []         # not resumed for a skipped net
    assert again.radios["ftm"]["listen"]["next"]["start"] == "2026-12-06T19:00"
    c2.t = datetime(2026, 12, 6, 18, 59)                                          # the next one is not skipped
    again.nets.tick()
    assert not again.radios["ftm"]["paused"]


def test_the_rule_is_honoured_everywhere(tmp_path):
    st, clock = make(tmp_path, datetime(2026, 11, 8, 19, 30))                    # 2nd Sunday: no net
    n = st.schedule_now()
    assert n["current"] is None and n["next"]["start"] == "2026-11-15T19:00" and n["next"]["retune"]
    clock.t = SUN.replace(hour=19, minute=30)
    n = st.schedule_now()
    assert n["current"]["name"] == "Sunday Net" and n["by_radio"]["ftm"]["ncs"] == "K7NET"
    # the radio is on another frequency (retune): what it hears is not the net's (owner 2026-11-11)
    st._tx_start("ftm", SUN.replace(hour=19, minute=20).timestamp())
    assert "net" not in st.tx[-1]
    # on the net's frequency, a transmission heard in the net is named after it (its history instance)
    st.set_radio("ftm", 146.94, "FM")
    st._tx_start("ftm", SUN.replace(hour=19, minute=20).timestamp())
    assert st.tx[-1]["net"] == {"key": "sun@2026-11-15", "id": "sun", "date": "2026-11-15", "name": "Sunday Net"}
    st._tx_start("ftm", SUN.replace(hour=21, minute=0).timestamp())               # long after it: a conversation
    assert "net" not in st.tx[-1]
    dec = st.nets.decorated()[0]
    assert dec["summary"] == "Every 1st and 3rd Sunday at 19:00, 146.940 MHz FM, 60 min"
    assert dec["next"]["date"] == "2026-12-06"


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def test_skip_move_restore_preview_and_alert_api(tmp_path):
    st, clock = make(tmp_path, datetime(2026, 11, 3, 12))
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        pv = _post(base, "/api/schedule/preview", {"name": "x", "radio": "ftm", "start": "19:00", "freq_mhz": "146.94",
                                                   "mode": "FM", "repeat": {"freq": "monthly", "nth": [1, 3],
                                                                            "weekdays": ["Sun"]}})
        assert pv["summary"].startswith("Every 1st and 3rd Sunday at 19:00") and len(pv["next"]) == 5
        assert pv["next"][0]["date"] == "2026-11-15"
        res = _post(base, "/api/schedule/sun/skip", {"date": "2026-11-15", "note": "Hamfest"})
        assert res["schedule"][0]["next"]["date"] == "2026-12-06"
        assert _post(base, "/api/schedule/preview", res["schedule"][0])["next"][0]["cancelled"]
        res = _post(base, "/api/schedule/sun/move", {"date": "2026-12-06", "start": "18:30", "freq_mhz": 147.0})
        assert res["schedule"][0]["next"]["start"] == "2026-12-06T18:30" and res["schedule"][0]["next"]["moved"]
        res = _post(base, "/api/schedule/sun/move", {"date": "2026-12-06", "to_date": "2026-12-05", "start": "18:30"})
        assert res["schedule"][0]["next"]["start"] == "2026-12-05T18:30"
        assert st.cfg["schedule"][0]["exceptions"]["2026-12-06"] == {"date": "2026-12-05", "start": "18:30"}
        # an edit from the editor (which does not send the exceptions) keeps them
        _post(base, "/api/schedule/sun", {k: v for k, v in res["schedule"][0].items()
                                          if k not in ("exceptions", "summary", "next")} | {"name": "Sunday ARES Net"})
        assert set(st.cfg["schedule"][0]["exceptions"]) == {"2026-11-15", "2026-12-06"}
        res = _post(base, "/api/schedule/sun/restore", {"date": "2026-11-15"})
        assert res["schedule"][0]["next"]["date"] == "2026-11-15"
        for path, body in (("/api/schedule/sun/skip", {"date": "2026-11-16"}),          # not a day it meets
                           ("/api/schedule/sun/skip", {})):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(base, path, body)
            assert e.value.code == 400, path
        bad = _post(base, "/api/schedule/preview", {"name": "x", "radio": "ftm", "start": "19:00",
                                                    "repeat": {"freq": "monthly"}})
        assert bad["error"] and "next" not in bad                 # a rule half typed: said in place, not a 400
        clock.t = SUN.replace(hour=18, minute=57)
        st.nets.tick()
        got = json.loads(urllib.request.urlopen(base + "/api/alerts").read())["alerts"]
        assert got[0]["key"] == "sun@2026-11-15"
        r = _post(base, "/api/alerts/sun%402026-11-15/ready", {})          # encoded as the page sends it
        assert r["alerts"][0]["state"] == "ready"
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(base, "/api/alerts/nope@2026-11-15/ready", {})
        assert e.value.code == 404
    finally:
        srv.shutdown()
