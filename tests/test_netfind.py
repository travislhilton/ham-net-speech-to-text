"""Find nets (station/netfind.py): where the station is, which bands its radios allow, the xAI web search answer
cleaned into nets, the HF reach estimate, and adding the ticked nets."""
import json

import pytest

from station import netfind as NF
from tests.test_agent import _station, xai_key  # noqa: F401


def _answer(nets, opened=()):
    return {"output": [{"type": "web_search_call", "action": {"type": "open_page", "url": u}} for u in opened]
            + [{"type": "message", "content": [{"type": "output_text",
                                                "text": "Here you go:\n```json\n" + json.dumps({"nets": nets}) + "\n```"}]}],
            "usage": {"cost_in_usd_ticks": 1_500_000_000}}


VHF = {"name": "Springfield ARES Net", "freq_mhz": 146.94, "mode": "FM", "offset_mhz": -0.6, "tone_hz": 103.5,
       "repeat": "weekly", "weekdays": ["Tue"], "start": "19:30", "time_zone": "local", "duration_min": 30,
       "area": "Springfield", "distance_km": 12, "emergency": True, "source_url": "https://example.org/nets/",
       "org": "County ARES"}
HFNET = {"name": "Section Phone Net", "freq_mhz": 3.915, "mode": "LSB", "repeat": "weekly", "weekdays": ["Mon"],
         "start": "01:00", "time_zone": "UTC", "distance_km": 250, "emergency": False,
         "source_url": "https://example.net/hf"}


def _finder(tmp_path, nets, opened=(), seen=None):
    st = _station(tmp_path)
    st._save({"operator": {"grid": "EN49ab"}})
    sent = seen if seen is not None else []

    def post(key, body, timeout):
        sent.append(body)
        return _answer(nets, opened)
    return st, NF.Finder(st, post=post)


def test_the_bands_follow_the_radios(tmp_path):
    st = _station(tmp_path)
    assert NF.reach(st)["HF"] and NF.reach(st)["VU"]               # an IC-705 does both (with the right antenna)
    st.radios["ic"]["model"] = "vu_fm"
    r = NF.reach(st)
    assert r["VU"] and not r["HF"] and r["modes"] == ["FM"]
    st.radios["ic"]["model"] = "hf"
    assert NF.reach(st)["HF"] and not NF.reach(st)["VU"]
    st.radios["ic"]["role"] = "winlink"
    assert not NF.reach(st)["HF"] and not NF.reach(st)["VU"]


def test_location_grid_first_then_this_pcs_position_then_the_address(tmp_path):
    st = _station(tmp_path)
    f = NF.Finder(st)
    assert NF.location(st) is None
    f.settings({"address": "12 Main St, Springfield, IL"})
    loc = NF.location(st)
    assert loc["source"] == "address" and loc["label"] == "Springfield, IL"   # the street is never kept for sending
    f.settings({"gps": {"lat": 39.7817, "lon": -89.6501, "acc_m": 40}})
    assert NF.location(st)["source"] == "gps"
    st._save({"operator": {"grid": "EN59"}})
    assert NF.location(st)["source"] == "grid"


def test_only_a_rounded_position_is_sent_never_the_address(tmp_path, xai_key):
    sent = []
    st, f = _finder(tmp_path, [VHF], seen=sent)
    st._save({})
    f.settings({"address": "12 Main St, Springfield, IL", "gps": {"lat": 39.78172, "lon": -89.65013}})
    f.search()
    text = sent[0]["input"][0]["content"]
    assert "latitude 39.8, longitude -89.7" in text and "Main St" not in text and "39.78" not in text
    assert "ARES" in text and "RACES" in text and "clubs" in text and sent[0]["tools"] == [{"type": "web_search"}]


def test_a_search_keeps_nets_with_a_source_and_drops_the_rest(tmp_path, xai_key):
    bad = [dict(VHF, name="No Source", source_url=""), dict(VHF, name="Far Away", distance_km=300),
           dict(VHF, name="No Time", start=""), dict(VHF, name="DMR Net", mode="DMR", freq_mhz=442.1)]
    st, f = _finder(tmp_path, [VHF, HFNET] + bad, opened=["https://example.org/nets"])
    s = f.search()
    names = [n["name"] for n in s["nets"]]
    assert names == ["Springfield ARES Net", "Section Phone Net"]
    v = s["nets"][0]
    assert v["emergency"] and v["source_opened"] and v["offset_mhz"] == -0.6 and v["tone_hz"] == 103.5
    assert v["radio"] == "ic" and v["org"] == "County ARES"
    assert not s["nets"][1]["source_opened"]
    assert len(s["left_out"]) == 4 and s["cost_usd"] == 0.15
    h = s["nets"][1]
    assert h["prop"]["chance"] in ("likely", "maybe", "unlikely") and "checkin" in h["prop"]


def test_a_vhf_only_station_never_gets_hf_nets(tmp_path, xai_key):
    sent = []
    st, f = _finder(tmp_path, [VHF, HFNET], seen=sent)
    st.radios["ic"]["model"] = "vu_fm"
    s = f.search()
    assert [n["name"] for n in s["nets"]] == ["Springfield ARES Net"]
    assert "HF nets (160-10 m)" not in sent[0]["input"][0]["content"]


def test_adding_the_ticked_nets_with_a_chosen_radio(tmp_path, xai_key):
    st, f = _finder(tmp_path, [VHF, HFNET])
    s = f.search()
    vid = s["nets"][0]["id"]
    before = len(st.cfg["schedule"])
    r = f.add([vid], {vid: ""})
    assert r["added"] == ["Springfield ARES Net"] and not r["failed"]
    n = next(x for x in st.cfg["schedule"] if x["name"] == "Springfield ARES Net")
    assert n["source"] == "https://example.org/nets/"
    st.nets.update(n["id"], {k: v for k, v in n.items() if k not in ("id", "source")})   # the editor keeps it
    n = next(x for x in st.cfg["schedule"] if x["name"] == "Springfield ARES Net")
    assert n["source"] == "https://example.org/nets/"
    assert n.get("radio") is None and n["freq_mhz"] == 146.94 and n["emergency"] and n["tone_hz"] == 103.5
    assert n["repeat"] == {"freq": "weekly", "weekdays": ["Tue"]} and len(st.cfg["schedule"]) == before + 1
    assert next(x for x in f.state()["nets"] if x["id"] == vid)["have"] == "Springfield ARES Net"


def test_no_key_and_no_location_are_said_plainly(tmp_path):
    st = _station(tmp_path)
    with pytest.raises(ValueError, match="xAI key"):
        NF.Finder(st).search()


def test_hf_reach_depends_on_antenna_power_and_time():
    P = NF.propagation
    assert P(3.9, 200, "19:00", "dipole_low", 100)["chance"] == "likely"          # 80 m NVIS, evening
    assert P(3.9, 200, "19:00", "whip", 10)["checkin"] == "unlikely"             # a whip at 10 W
    assert P(7.2, 200, "21:00", "efhw", 100)["chance"] == "unlikely"             # 40 m skips short paths at night
    assert P(14.3, 1500, "11:00", "vertical", 100)["chance"] == "likely"         # 20 m daytime, long path
    assert P(14.3, 200, "11:00", "beam", 100)["chance"] == "unlikely"            # 20 m skips over 200 km
    a, b = P(3.9, 300, "20:00", "efhw", 5), P(3.9, 300, "20:00", "efhw", 100)
    assert a["chance"] == b["chance"] and a["checkin"] != "likely"                # power: checking in, not hearing


def test_times_in_another_zone_or_utc_become_this_pcs_time():
    from datetime import datetime
    here = datetime.now().astimezone().utcoffset().total_seconds() / 3600
    days, t = NF._to_pc(["Mon"], "12:00", here)
    assert (days, t) == (["Mon"], "12:00")                          # same zone: unchanged
    days, t = NF._to_pc(["Mon"], "12:00", here - 1)                 # an hour west of here: 13:00 our time
    assert (days, t) == (["Mon"], "13:00")
    st_days, st = NF._to_pc(["Mon"], "23:30", here - 2)             # late evening two hours west: after midnight here
    assert (st_days, st) == (["Tue"], "01:30")

