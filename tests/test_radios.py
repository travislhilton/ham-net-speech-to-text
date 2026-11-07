"""Radios…: inputs offered, exact-name device choice, the saved list, the dashboard routes."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from station import radios
from station.server import serve
from tests.test_station import station  # noqa: F401 — the fixture

APIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
DEVS = [  # a laptop station with two Digirig Lites, one renamed for the net (2026-11-05)
    {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
    {"name": "Digirig Lite NET-REC (USB Audio", "max_input_channels": 1, "hostapi": 0},
    {"name": "Digirig Lite (USB Audio Device)", "max_input_channels": 1, "hostapi": 0},
    {"name": "Speakers (Realtek)", "max_input_channels": 0, "hostapi": 0},
    {"name": "Digirig Lite NET-REC (USB Audio Device)", "max_input_channels": 1, "hostapi": 2},
    {"name": "Digirig Lite (USB Audio Device)", "max_input_channels": 1, "hostapi": 2},
    {"name": "Microphone Array (Intel Smart Sound)", "max_input_channels": 2, "hostapi": 2},
    {"name": "Input ()", "max_input_channels": 2, "hostapi": 3},
]


def test_inputs_one_per_device():
    assert radios.inputs(DEVS, APIS) == ["Digirig Lite", "Digirig Lite NET-REC", "Microphone Array"]


def test_exact_name_wins_so_the_winlink_adapter_is_never_opened_by_mistake():
    assert radios.pick_index("Digirig Lite", DEVS) == 2
    assert radios.pick_index("Digirig Lite NET-REC", DEVS) == 1
    assert radios.pick_index("NET-REC", DEVS) == 1                         # old partial names still work
    assert radios.pick_index("IC-705", DEVS) is None
    names = ["Digirig Lite NET-REC (USB Audio Device)", "Digirig Lite (USB Audio Device)"]
    assert radios.best_endpoint("Digirig Lite", names) == names[1]
    assert radios.best_endpoint("net-rec", names) == names[0]


def test_clean_checks_and_fills_ids():
    out = radios.clean([{"name": "Net radio", "device": "Digirig Lite NET-REC", "gate": "voice", "freq_mhz": "146.94"},
                        {"name": "Net radio", "device": "IC-705", "gate": "voice", "mode": "lsb"}])
    assert [r["id"] for r in out] == ["net-radio", "net-radio-2"] and out[0]["freq_mhz"] == 146.94
    assert out[1]["mode"] == "LSB" and out[1]["freq_mhz"] is None
    for bad, msg in (([{"name": "", "device": "x"}], "name"), ([{"name": "A"}], "sound input"),
                     ([{"name": "A", "device": "x"}, {"name": "B", "device": "X"}], "another radio"),
                     ([{"name": "A", "device": "x", "freq_mhz": "abc"}], "MHz")):
        with pytest.raises(ValueError, match=msg):
            radios.clean(bad)


def test_load_keeps_settings_the_dashboard_does_not_edit(tmp_path):
    base = [{"id": "ftm", "name": "FTM", "device": "NET-REC", "gate": "carrier", "meter": {"low_dbfs": -50}}]
    assert radios.load(tmp_path / "radios.json", base) == base
    radios.save(tmp_path / "radios.json", [{"id": "ftm", "name": "Mobile", "device": "Digirig Lite NET-REC",
                                            "gate": "voice", "mode": "FM", "freq_mhz": None}])
    got = radios.load(tmp_path / "radios.json", base)
    assert got == [{"meter": {"low_dbfs": -50}, "id": "ftm", "name": "Mobile", "device": "Digirig Lite NET-REC",
                    "gate": "voice", "mode": "FM"}]


def test_radio_list_routes(station):  # noqa: F811
    station.no_restart = True
    srv = serve(station, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda body: urllib.request.urlopen(urllib.request.Request(
        base + "/api/radio-list", json.dumps(body).encode(), {"Content-Type": "application/json"}))
    try:
        d = json.loads(urllib.request.urlopen(base + "/api/radio-list").read())
        assert d["radios"][0]["id"] == "ftm" and d["from"] == "station.yaml" and d["nets"]["ftm"] == ["Test net"]
        with pytest.raises(urllib.error.HTTPError) as e:              # its net would be orphaned
            post({"radios": [{"name": "Other", "device": "IC-705"}]})
        assert e.value.code == 400 and "Nets" in json.loads(e.value.read())["error"]
        out = json.loads(post({"radios": [{"id": "ftm", "name": "Mobile", "device": "Digirig Lite NET-REC",
                                           "gate": "voice"}, {"name": "HF", "device": "IC-705", "gate": "voice",
                                                              "mode": "LSB"}]}).read())
        assert out["ok"] and [r["id"] for r in out["radios"]] == ["ftm", "hf"]
        assert (station.data / "radios.json").exists()
        assert json.loads(urllib.request.urlopen(base + "/api/radio-list").read())["from"] == "dashboard"
    finally:
        srv.shutdown()


def test_keep_awake_is_a_dashboard_setting(station):  # noqa: F811
    assert station.operator()["keep_awake"] == "traffic"                       # the default since 2026-11-05
    assert station.set_operator({"callsign": "W9XYZ", "keep_awake": "listening"})["keep_awake"] == "listening"
    assert station.awake_mode == "listening"
    with pytest.raises(ValueError, match="keep awake"):
        station.set_operator({"callsign": "W9XYZ", "keep_awake": "sometimes"})


def test_a_driver_update_that_renamed_the_input_is_explained():
    from station.radios import why_missing
    eps = [{"name": "Digirig Lite NET-REC", "port": "USB\\A", "present": False},
           {"name": "Microphone", "port": "USB\\A", "present": True},
           {"name": "Digirig Lite", "port": "USB\\B", "present": False}]
    msg = why_missing("Digirig Lite NET-REC", eps)
    assert "Rename that input to 'Digirig Lite NET-REC'" in msg and "'Microphone'" in msg
    assert why_missing("Digirig Lite", eps) == "the input 'Digirig Lite' is not plugged in"
    assert why_missing("Nothing", eps) == ""
