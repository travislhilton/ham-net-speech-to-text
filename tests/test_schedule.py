"""The net schedule edited on the dashboard (data/schedule.json) and "listen only during scheduled nets"."""
import copy
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from station import engine
from station.core import Station
from station.schedule import NoSuchNet, listen_window, validate
from station.server import serve

CFG = {"callsign": "W9XYZ",
       "radios": [{"id": "ftm", "name": "FTM-150R", "device": "NET-REC", "gate": "carrier", "freq_mhz": 146.94},
                  {"id": "ic705", "name": "IC-705", "device": "IC-705", "gate": "voice", "freq_mhz": 7.185, "mode": "LSB"}],
       "schedule": [{"name": "Example County ARES net", "radio": "ftm", "freq_mhz": 146.94, "days": ["Tue"],
                     "start": "20:00", "duration_min": 60, "ncs": "K7NET"}],
       "port": 0, "bind": "127.0.0.1", "qrz_online": False}
TUE = datetime(2026, 11, 3)          # a Tuesday


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, clock=None, cfg=CFG):
    return Station(copy.deepcopy(cfg), lambda: engine.FakeEngine(), data_dir=tmp_path,
                   clock=clock or Clock(TUE.replace(hour=12)))


def test_validate_normalizes_and_refuses():
    radios = {"ftm": {}, "ic705": {}}
    v = validate({"name": " HF net ", "radio": "ic705", "freq_mhz": "3,915", "mode": "lsb", "days": "thursday, mon",
                  "start": "7:05", "duration_min": "90", "ncs": "k7abc"}, radios)
    assert v == {"name": "HF net", "radio": "ic705", "freq_mhz": 3.915, "mode": "LSB",       # old form, new rule
                 "repeat": {"freq": "weekly", "weekdays": ["Mon", "Thu"]}, "alert_min": 5,
                 "start": "07:05", "duration_min": 90, "ncs": "K7ABC"}
    ok = {"name": "n", "radio": "ftm", "days": ["Tue"], "start": "20:00", "duration_min": 60}
    assert "freq_mhz" not in validate(ok, radios)                         # optional: the radio's own frequency
    for bad in ({"radio": "nope"}, {"start": "24:00"}, {"start": "8pm"}, {"start": "20:61"}, {"days": ["Tuz"]},
                {"days": []}, {"duration_min": 0}, {"duration_min": 1441}, {"duration_min": "x"},
                {"freq_mhz": 99999}, {"freq_mhz": "abc"}, {"mode": "SSB"}, {"ncs": "not a call"}, {"name": ""}):
        with pytest.raises(ValueError):
            validate(ok | bad, radios)


def test_seeded_from_station_yaml_then_stored_in_data(tmp_path):
    st = make(tmp_path)
    assert [n["name"] for n in st.cfg["schedule"]] == ["Example County ARES net"] and st.cfg["schedule"][0]["id"]
    assert not (tmp_path / "schedule.json").exists()                     # nothing written until an edit
    q = st.bus.subscribe()
    res = st.nets.add({"name": "HF net", "radio": "ic705", "freq_mhz": 3.915, "mode": "LSB", "days": ["Thu"],
                       "start": "19:30", "duration_min": 45})
    assert len(res["schedule"]) == 2 and res["edited"] and (tmp_path / "schedule.json").exists()
    kind, data = q.get(timeout=1)
    while kind != "schedule":
        kind, data = q.get(timeout=1)
    assert kind == "schedule" and len(data["schedule"]) == 2 and "net" in data         # every browser updates
    nid = res["id"]
    st.nets.update(nid, {"name": "HF net", "radio": "ic705", "days": ["Fri"], "start": "19:30", "duration_min": 45})
    assert st.cfg["schedule"][1]["repeat"]["weekdays"] == ["Fri"] and "freq_mhz" not in st.cfg["schedule"][1]
    with pytest.raises(NoSuchNet):
        st.nets.update("nope", {"name": "x", "radio": "ftm", "start": "10:00"})
    with pytest.raises(ValueError):
        st.nets.add({"name": "x", "radio": "ftm", "start": "25:00"})
    first = st.cfg["schedule"][0]["id"]
    st.nets.delete(first)
    # a restart reads data/schedule.json, not station.yaml's entry (which is gone now)
    again = make(tmp_path)
    assert [n["name"] for n in again.cfg["schedule"]] == ["HF net"] and again.cfg["schedule"][0]["id"] == nid
    # and schedule_now() uses the edited schedule with no restart
    st.nets.add({"name": "Noon net", "radio": "ftm", "days": ["Tue"], "start": "11:30", "duration_min": 60})
    assert st.schedule_now()["current"]["name"] == "Noon net"


def test_listen_window_lead_and_grace():
    nets = [{"id": "a", "name": "ARES", "radio": "ftm", "days": ["Tue"], "start": "20:00", "duration_min": 60}]
    cur, nxt = listen_window(nets, "ftm", TUE.replace(hour=19, minute=58))
    assert cur is None and nxt["name"] == "ARES" and nxt["start"] == "2026-11-03T20:00"
    assert listen_window(nets, "ftm", TUE.replace(hour=19, minute=59))[0]["name"] == "ARES"       # 1 min early
    assert listen_window(nets, "ftm", TUE.replace(hour=21, minute=4))[0]["name"] == "ARES"        # 5 min grace
    cur, nxt = listen_window(nets, "ftm", TUE.replace(hour=21, minute=5))
    assert cur is None and nxt["start"] == "2026-11-10T20:00"                                     # next week
    assert listen_window(nets, "ic705", TUE.replace(hour=20, minute=30)) == (None, None)          # other radio


def test_listen_only_during_nets_pauses_and_resumes(tmp_path):
    clock = Clock(TUE.replace(hour=12))
    st = make(tmp_path, clock)
    st.set_radio("ftm", 145.9, "FM")
    r = st.nets.set_listen("ftm", {"scheduled": True, "tune": True})
    assert r["paused"] and r["paused_by"] == "schedule"                   # no net now: paused at once
    assert r["listen"]["next"] == {"name": "Example County ARES net", "start": "2026-11-03T20:00"}
    assert not st.radios["ic705"].get("paused")                             # the other radio is untouched
    clock.t = TUE.replace(hour=19, minute=58)                             # 2 min before: still paused
    st.nets.tick()
    assert st.radios["ftm"]["paused"]
    clock.t = TUE.replace(hour=19, minute=59)                             # 1 min before: listening
    st.nets.tick()
    assert not st.radios["ftm"]["paused"] and st.radios["ftm"]["paused_by"] is None
    assert st.radios["ftm"]["freq_mhz"] == 146.94                         # logged as the net's frequency
    assert st.radios["ftm"]["listen"]["now"] == "Example County ARES net"
    # the operator pauses during the net: it stays paused (no change of window)
    st.set_paused("ftm", True)
    clock.t = TUE.replace(hour=20, minute=30)
    st.nets.tick()
    assert st.radios["ftm"]["paused"] and st.radios["ftm"]["paused_by"] == "operator"
    # ... and resumes by hand; at the end (+ grace) the schedule pauses it again
    st.set_paused("ftm", False)
    clock.t = TUE.replace(hour=21, minute=4)
    st.nets.tick()
    assert not st.radios["ftm"]["paused"]
    clock.t = TUE.replace(hour=21, minute=5)
    st.nets.tick()
    assert st.radios["ftm"]["paused"] and st.radios["ftm"]["paused_by"] == "schedule"
    # an operator's Resume outside a net holds until the next start/end
    st.set_paused("ftm", False)
    clock.t = TUE.replace(hour=22)
    st.nets.tick()
    assert not st.radios["ftm"]["paused"]
    # the settings and the reason survive a restart
    st.set_paused("ftm", True, by="schedule")
    again = make(tmp_path, Clock(TUE.replace(hour=22)))
    assert again.radios["ftm"]["listen"]["scheduled"] and again.radios["ftm"]["listen"]["tune"]
    assert again.radios["ftm"]["paused_by"] == "schedule"
    # turning it off leaves the radio as it is and stops acting
    st.nets.set_listen("ftm", {"scheduled": False})
    clock.t = TUE.replace(day=10, hour=20, minute=10)
    st.nets.tick()
    assert st.radios["ftm"]["paused"]


def test_restart_inside_a_net_keeps_the_operators_pause(tmp_path):
    st = make(tmp_path, Clock(TUE.replace(hour=20, minute=10)))
    st.nets.set_listen("ftm", {"scheduled": True})
    st.set_paused("ftm", True)                                            # paused by the operator mid-net
    again = make(tmp_path, Clock(TUE.replace(hour=20, minute=20)))
    again.nets.tick()
    assert again.radios["ftm"]["paused"] and again.radios["ftm"]["paused_by"] == "operator"
    again.nets.tick(TUE.replace(hour=21, minute=10))
    again.nets.tick(TUE.replace(day=10, hour=19, minute=59))              # next week's net resumes it
    assert not again.radios["ftm"]["paused"]


def _post(base, path, body, ctype="application/json"):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": ctype})
    return json.loads(urllib.request.urlopen(req).read())


def test_schedule_api(tmp_path):
    st = make(tmp_path)
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        got = json.loads(urllib.request.urlopen(base + "/api/schedule").read())
        assert len(got["schedule"]) == 1 and {r["id"] for r in got["radios"]} == {"ftm", "ic705"} and not got["edited"]
        res = _post(base, "/api/schedule", {"name": "HF net", "radio": "ic705", "days": ["Thu"], "start": "19:30",
                                            "duration_min": 45})
        nid = res["id"]
        res = _post(base, f"/api/schedule/{nid}", {"name": "HF net", "radio": "ic705", "days": ["Thu"],
                                                   "start": "19:45", "duration_min": 45})
        assert [n["start"] for n in res["schedule"] if n["id"] == nid] == ["19:45"]
        for path, body, code in (("/api/schedule", {"name": "x", "radio": "nope", "start": "10:00"}, 400),
                                 ("/api/schedule", {"name": "x", "radio": "ftm", "start": "99:00"}, 400),
                                 ("/api/schedule/nope", {"name": "x", "radio": "ftm", "start": "10:00"}, 404),
                                 ("/api/schedule/nope/delete", {}, 404),
                                 ("/api/radios/nope/listen", {"scheduled": True}, 404)):
            with pytest.raises(urllib.error.HTTPError) as e:
                _post(base, path, body)
            assert e.value.code == code, path
        with pytest.raises(urllib.error.HTTPError) as e:                   # JSON only
            _post(base, "/api/schedule", {}, "application/x-www-form-urlencoded")
        assert e.value.code == 404
        r = _post(base, "/api/radios/ftm/listen", {"scheduled": True, "tune": False})
        assert r["listen"]["scheduled"] and r["paused"] and r["paused_by"] == "schedule"
        res = _post(base, f"/api/schedule/{nid}/delete", {})
        assert nid not in {n["id"] for n in res["schedule"]}
        assert json.loads(urllib.request.urlopen(base + "/api/state").read())["schedule"] == res["schedule"]
    finally:
        srv.shutdown()
