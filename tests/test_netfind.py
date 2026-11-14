"""Find nets (station/netfind.py): where the station is, which bands its radios allow, the xAI web search answer
cleaned into nets, the HF reach estimate, and adding the ticked nets."""
import json

import pytest

from station import hfprop as HP
from station import netfind as NF
from tests.test_agent import _station, xai_key  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):                                         # NOAA's solar flux: never fetched in tests
    def no(url):
        raise OSError("offline")
    monkeypatch.setattr(HP, "_fetch", no)


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
    assert len(s["left_out"]) == 4 and s["cost_usd"] == 0.3        # two searches (VHF/UHF and HF), in parallel
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
    s2 = f.state()                                                 # added: no longer offered, listed as scheduled
    assert vid not in [x["id"] for x in s2["nets"]] and s2["scheduled"] == [{"name": "Springfield ARES Net",
                                                                             "as": "Springfield ARES Net"}]


def test_no_key_and_no_location_are_said_plainly(tmp_path):
    st = _station(tmp_path)
    with pytest.raises(ValueError, match="xAI key"):
        NF.Finder(st).search()


def test_hf_reach_follows_the_antennas_pattern_the_hour_and_power():
    E = HP.estimate
    assert E(3.9, 300, "19:30", "dipole", 6, 100, 140)["chance"] == "likely"      # 80 m NVIS at night, low dipole
    assert E(7.2, 300, "21:00", "dipole", 10, 100, 140)["chance"] == "unlikely"   # 40 m skips over 300 km at night
    assert E(7.2, 300, "12:00", "dipole", 10, 100, 140)["chance"] == "likely"     # ... and covers it by day
    assert E(14.3, 1800, "13:00", "dipole", 10, 100, 140)["chance"] == "likely"   # 20 m daytime, long path
    assert E(14.3, 1800, "23:00", "dipole", 10, 100, 140)["chance"] == "unlikely" # 20 m closed at night
    assert E(14.3, 300, "13:00", "beam", 15, 100, 140)["chance"] == "unlikely"    # a beam fires too low for 300 km
    w = E(3.9, 300, "19:30", "whip", 0, 100, 140)
    assert w["checkin"] == "unlikely" and w["snr_hear"] > w["snr_send"]           # a whip hears better than it is heard
    a, b = E(3.9, 300, "19:30", "efhw", 10, 5, 140), E(3.9, 300, "19:30", "efhw", 10, 100, 140)
    assert a["snr_hear"] == b["snr_hear"] and a["snr_send"] < b["snr_send"]       # power: checking in, not hearing


def test_the_antenna_pattern_low_dipole_fires_up_high_dipole_and_vertical_fire_low():
    g = HP.gain
    assert g("dipole", 3.9, 6, 80) > g("dipole", 3.9, 6, 15) + 10                 # 0.08 wave up: NVIS
    assert g("dipole", 14.2, 20, 12) > g("dipole", 14.2, 20, 80)                  # 1 wave up: low angles
    assert g("vertical", 7.2, 0, 15) > g("vertical", 7.2, 0, 75) + 8              # a vertical: nothing straight up
    assert g("whip", 3.9, 0, 30) < g("vertical", 3.9, 0, 30) - 15                 # a short whip on 80 m
    assert g("beam", 7.2, 15, 20) <= -20                                          # a 20-10 m beam on 40 m


def test_solar_flux_from_noaa_cached_and_offline_fallback():
    c = {}
    v, src = HP.solar_flux(c, fetch=lambda u: [{"time_tag": "2026-11-12", "flux": 150}, {"time_tag": "2026-11-13", "flux": 162.4}])
    assert v == 162 and src == "NOAA, today" and c["sfi"] == 162
    v, _ = HP.solar_flux(c, fetch=lambda u: 1 / 0)                                # cached: not fetched again
    assert v == 162
    assert HP.solar_flux({}, fetch=lambda u: 1 / 0) == (130.0, "typical (offline)")


def test_settings_sent_with_the_search_are_used_and_one_band_keeps_the_other(tmp_path, xai_key):
    sent = []
    st, f = _finder(tmp_path, [VHF, HFNET], seen=sent)
    f.search()
    assert {n["name"] for n in f.state()["nets"]} == {"Springfield ARES Net", "Section Phone Net"}
    sent.clear()
    s = f.search("HF", {"hf_km": 2000, "hf": {"antenna": "vertical", "height_m": 0, "power_w": 100},
                        "interests": ["traffic", "special"]})
    assert len(sent) == 1 and "2000 km" in sent[0]["input"][0]["content"]
    assert "National Traffic System" in sent[0]["input"][0]["content"] and "Hurricane" not in sent[0]["input"][0]["content"]
    assert {n["name"] for n in s["nets"]} == {"Springfield ARES Net", "Section Phone Net"}   # VHF kept from before
    assert s["hf"]["antenna"] == "vertical" and s["by_band"]["HF"]["km"] == 2000


def test_a_regional_hf_net_far_away_is_kept_when_it_covers_the_station(tmp_path, xai_key):
    far = dict(HFNET, name="Region Net", distance_km=1500, serves_station_area=True)
    st, f = _finder(tmp_path, [far, dict(HFNET, name="Distant Club Net", distance_km=1500)])
    names = [n["name"] for n in f.search("HF")["nets"]]
    assert names == ["Region Net"]


def test_times_in_another_zone_or_utc_become_this_pcs_time():
    from datetime import datetime
    here = datetime.now().astimezone().utcoffset().total_seconds() / 3600
    days, t = NF._to_pc(["Mon"], "12:00", here)
    assert (days, t) == (["Mon"], "12:00")                          # same zone: unchanged
    days, t = NF._to_pc(["Mon"], "12:00", here - 1)                 # an hour west of here: 13:00 our time
    assert (days, t) == (["Mon"], "13:00")
    st_days, st = NF._to_pc(["Mon"], "23:30", here - 2)             # late evening two hours west: after midnight here
    assert (st_days, st) == (["Tue"], "01:30")


def test_distance_is_computed_from_the_nets_town_and_a_proven_range_shapes_hf_reach(tmp_path, xai_key):
    near = dict(HFNET, name="Close Net", freq_mhz=7.2, start="12:00", time_zone="local", lat=40.3, lon=-89.6,
                distance_km=999)                                   # ~55 km from the grid square: the search's 999 ignored
    mid = dict(near, name="Sweet Spot Net", lat=43.6, lon=-89.0)  # ~ 450 km north
    st, f = _finder(tmp_path, [near, mid])
    st._save({})
    f.settings({"gps": {"lat": 39.97, "lon": -89.96}})             # the example area
    f.settings({"hf": {"antenna": "dipole", "height_m": 8, "power_w": 50, "good_from_km": 400, "good_to_km": 600}})
    s = f.search("HF")
    d = {n["name"]: n for n in s["nets"]}
    assert d["Close Net"]["dist_src"] == "computed" and d["Close Net"]["distance_km"] < 80
    assert d["Sweet Spot Net"]["prop"]["pct_hear"] > d["Close Net"]["prop"]["pct_hear"]
    with pytest.raises(ValueError, match="both distances"):
        f.settings({"hf": {"antenna": "dipole", "power_w": 50, "good_from_km": 400}})



def test_the_sun_over_the_path_when_the_net_meets_decides_day_or_night_and_season():
    from datetime import datetime, timezone
    z = HP.solar_zenith
    assert abs(z(datetime(2026, 6, 21, 18, 0, tzinfo=timezone.utc), 40, -90) - 17) < 2    # local noon, June
    assert abs(z(datetime(2026, 12, 21, 18, 0, tzinfo=timezone.utc), 40, -90) - 63) < 2   # local noon, December
    E = HP.estimate
    nov = E(7.2, 600, "19:30", "dipole", 8, 50, 118, (400, 600), datetime(2026, 11, 17, 2, 30, tzinfo=timezone.utc), (38.5, -113))
    jun = E(7.2, 600, "19:30", "dipole", 8, 50, 118, (400, 600), datetime(2026, 6, 17, 1, 30, tzinfo=timezone.utc), (38.5, -113))
    assert nov["dark"] and not jun["dark"] and jun["muf"] > nov["muf"]                   # a June evening is still day
    noon = E(3.9, 300, "12:00", "dipole", 8, 100, 118, None, datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc), (40, -90))
    night = E(3.9, 300, "22:00", "dipole", 8, 100, 118, None, datetime(2026, 6, 18, 4, 0, tzinfo=timezone.utc), (40, -90))
    assert night["snr_hear"] > noon["snr_hear"]                                           # 80 m: D-layer absorption by day


def test_antenna_gains_match_the_published_models_within_a_db_or_two():
    g, lam = HP.gain, 300 / 3.9
    assert abs(g("dipole", 3.9, 0.04 * lam, 90) - (-0.5)) < 1.5              # NEC-4 (Cebik): low dipoles, zenith
    assert abs(g("dipole", 3.9, 0.12 * lam, 90) - 5.6) < 1.5
    assert abs(g("dipole", 3.9, 0.20 * lam, 90) - 6.4) < 1.5
    assert g("invv", 7.2, 12, 50) < g("dipole", 7.2, 12, 50)                  # an inverted V: a bit lower and lower gain
    assert g("vertical", 7.2, 0, 26, "average") > g("vertical", 7.2, 0, 75, "average") + 8
    assert g("vertical", 7.2, 0, 6, "salt") > g("vertical", 7.2, 0, 6, "poor") + 8   # salt water fires low
    w = [g("whip", f, 0, 30) - g("vertical", f, 0, 30) for f in (3.9, 7.2, 14.2)]
    assert w[0] < w[1] < w[2] < 0                                              # whip efficiency improves going up

