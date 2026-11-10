"""station/fit.py: can this radio record this net (band, antenna, mode, CAT, Winlink), clashes and how they settle."""
from datetime import datetime

from station import fit

TUE = datetime(2026, 11, 10, 12, 0)
HT = {"id": "ht", "name": "Handheld", "model": "vu_fm", "freq_mhz": 442.1, "mode": "FM", "role": "nets"}
IC = {"id": "ic", "name": "IC-705", "model": "ic705", "antenna": "hf", "freq_mhz": 3.915, "mode": "LSB", "role": "nets"}


def net(nid, f, mode, radio, start="20:00", dur=60, days=("Tue",), **kw):
    return {"id": nid, "name": nid, "freq_mhz": f, "mode": mode, "radio": radio, "start": start,
            "duration_min": dur, "rule": {"freq": "weekly", "days": list(days)}} | kw


def test_a_radio_that_is_on_the_frequency_records_and_one_that_is_not_asks_you_to_tune():
    assert fit.fit(net("a", 442.1, "FM", "ht"), HT)["kind"] == "ready"
    assert fit.fit(net("a", 146.52, "FM", "ht"), HT)["kind"] == "tune"
    assert fit.fit(net("a", 146.52, "FM", "ht"), HT | {"model": "ic705", "antenna": "vu"}, has_cat=True)["kind"] == "ready"


def test_band_mode_antenna_and_winlink_say_cant_and_why():
    assert fit.fit(net("a", 3.915, "LSB", "ht"), HT)["kind"] == "cant"           # no HF on a 2 m / 70 cm radio
    assert fit.fit(net("a", 442.1, "LSB", "ht"), HT)["why"].endswith("has no LSB")
    f = fit.fit(net("a", 442.1, "FM", "ic"), IC)
    assert f["kind"] == "cant" and f["antenna"] == "vu" and "VHF/UHF" in f["why"]
    assert fit.fit(net("a", 442.1, "FM", "ic"), IC | {"antenna": "vu"}, has_cat=True)["kind"] == "ready"
    assert fit.fit(net("a", 442.1, "FM", "ht"), HT | {"role": "winlink"})["kind"] == "cant"
    assert fit.fit(net("a", 442.1, "FM", None), None)["kind"] == "none"


def test_old_radios_get_a_model_from_their_name():
    assert fit.infer_model({"name": "IC-705", "device": "IC-705 (Remote Utility)"}) == "ic705"
    assert fit.infer_model({"name": "FTM-150R"}) == "vu_fm"
    assert fit.infer_model({"name": "Net radio (handheld)"}) == "vu_fm"
    assert fit.infer_model({"name": "Shack"}) == "any"


def test_two_nets_on_one_radio_clash_until_one_is_chosen_and_another_radio_is_suggested():
    radios = {"ht": HT, "ic": IC | {"antenna": "vu"}}
    nets = [net("a", 442.1, "FM", "ht"), net("b", 442.1, "FM", "ht", start="20:30")]
    st = fit.statuses(nets, radios, TUE, has_cat=lambda r: r == "ic")
    assert st["a"]["kind"] == st["b"]["kind"] == "clash" and st["a"]["clash"]["id"] == "b"
    assert st["a"]["suggest"] == "ic" and st["a"]["fits"]["ic"]["kind"] == "ready"
    nets[0]["beats"] = ["b"]
    st = fit.statuses(nets, radios, TUE)
    assert st["a"]["kind"] == "ready" and st["a"]["settled"][0]["wins"] is True
    assert st["b"]["kind"] == "ready" and st["b"]["settled"][0]["wins"] is False
    assert fit.loses_at(nets[1], nets, datetime(2026, 11, 10, 20, 40))
    assert not fit.loses_at(nets[1], nets, datetime(2026, 11, 17, 18, 0))


def test_a_net_that_cant_be_heard_does_not_clash():
    nets = [net("a", 442.1, "FM", "ic"), net("b", 3.915, "LSB", "ic")]
    st = fit.statuses(nets, {"ic": IC}, TUE, has_cat=lambda r: True)
    assert st["a"]["kind"] == "cant" and st["b"]["kind"] == "ready"


def test_the_station_moves_a_net_settles_a_clash_and_keeps_both_through_an_edit(tmp_path):
    from tests.test_cat import CFG, Clock, TUE as T
    import copy
    from station import engine
    from station.core import Station
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T.replace(hour=12)))
    a, b = (n["id"] for n in st.cfg["schedule"])
    d = st.nets.post(f"/api/schedule/{a}/radio", {"radio": None})
    assert next(n for n in d["schedule"] if n["id"] == a)["status"]["kind"] == "none"
    st.nets.post(f"/api/schedule/{a}/radio", {"radio": "ic"})
    st.nets.post(f"/api/schedule/{b}/settle", {"over": a})
    net_b = next(n for n in st.cfg["schedule"] if n["id"] == b)
    assert net_b["beats"] == [a]
    st.nets.update(b, {k: v for k, v in net_b.items() if k != "beats"} | {"name": "Renamed"})
    assert next(n for n in st.cfg["schedule"] if n["id"] == b)["beats"] == [a]    # the editor does not resend it
    st.nets.post(f"/api/schedule/{b}/settle", {"over": a, "undo": True})
    assert not next(n for n in st.cfg["schedule"] if n["id"] == b).get("beats")


def test_one_date_moved_to_another_radio_is_listened_to_there_only(tmp_path):
    from tests.test_cat import CFG, Clock, TUE as T
    import copy
    from station import engine
    from station.core import Station
    from station.schedule import listen_window
    cfg = copy.deepcopy(CFG)
    cfg["radios"].append({"id": "ht", "name": "Handheld", "device": "Digirig", "gate": "voice", "freq_mhz": 442.1,
                          "mode": "FM"})
    st = Station(cfg, lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T.replace(hour=12)))
    st.set_radio_setup("ic", {"antenna": "vu"})
    a = st.cfg["schedule"][0]                                    # the Tuesday 442.100 net on the IC-705
    st.nets.post(f"/api/schedule/{a['id']}/move", {"date": T.date().isoformat(), "radio": "ht"})
    nets, at = st.cfg["schedule"], T.replace(hour=20, minute=10)
    assert listen_window(nets, "ht", at)[0]["id"] == a["id"] and listen_window(nets, "ht", at)[0]["radio"] == "ht"
    assert listen_window(nets, "ic", at)[0] is None                      # not on its own radio that night
    nxt = T.replace(day=T.day + 7, hour=20, minute=10)
    assert listen_window(nets, "ic", nxt)[0]["id"] == a["id"] and listen_window(nets, "ht", nxt)[0] is None
    st.nets.post(f"/api/schedule/{a['id']}/move", {"date": T.date().isoformat(), "radio": "ic"})   # back: no exception
    assert not next(n for n in st.cfg["schedule"] if n["id"] == a["id"]).get("exceptions")


def test_a_quiet_net_ranks_after_another_net_on_at_the_same_time():
    from datetime import datetime
    from station.schedule import listen_window
    a = {"id": "a", "name": "A", "radio": "ic", "days": ["tue"], "start": "19:00", "duration_min": 60, "freq_mhz": 3.915}
    b = {"id": "b", "name": "B", "radio": "ic", "days": ["tue"], "start": "19:00", "duration_min": 60, "freq_mhz": 7.272}
    at = datetime(2026, 11, 10, 19, 15)
    first = listen_window([a, b], "ic", at)[0]
    other = listen_window([a, b], "ic", at, dead={first["key"]})[0]
    assert other["id"] != first["id"]
    # every candidate quiet: it stays on one rather than going silent
    assert listen_window([a, b], "ic", at, dead={"a@2026-11-10", "b@2026-11-10"})[0] is not None


def test_a_net_not_heard_three_times_is_flagged_until_kept(tmp_path):
    from tests.test_cat import CFG, Clock, TUE as T
    import copy
    from datetime import timedelta
    from station import engine
    from station.core import Station
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T))
    s, nid = st.nets, st.cfg["schedule"][0]["id"]
    for i in range(3):
        at = T + timedelta(days=7 * i)
        s._quiet["ic"] = {"key": f"{nid}@{at.date().isoformat()}", "since": at, "dead": set()}
        s._heard_end("ic", f"{nid}@{at.date().isoformat()}", at + timedelta(minutes=30))
    h = s.heard_of(nid)
    assert h["run"] == 3 and h["flag"]
    s.keep(nid)
    assert not s.heard_of(nid)["flag"]
    assert (tmp_path / "net_heard.json").exists()


def test_a_net_silent_for_five_minutes_hands_the_radio_to_the_net_beside_it(tmp_path):
    """Owner 11-10: "if quiet move to the next scheduled net. Look for 5 minute silence not 10" — counted from the
    last transmission, so a net that ends early frees the radio too."""
    import copy
    from datetime import timedelta
    from station import engine
    from station.cat import CatControl, Icom
    from station.core import Station
    from tests.test_cat import CFG, Clock, FakeIC705, TUE, edge
    cfg = copy.deepcopy(CFG)
    cfg["schedule"] = [{"name": "Local net", "radio": "ic", "freq_mhz": 3.906, "mode": "LSB", "days": ["Wed"],
                        "start": "19:00", "duration_min": 60},
                       {"name": "160 m net", "radio": "ic", "freq_mhz": 1.96, "mode": "LSB", "days": ["Wed"],
                        "start": "19:00", "duration_min": 60}]
    wed = TUE + timedelta(days=1)
    radio, clock = FakeIC705(freq=7_185_000, mode=0x00), Clock(wed.replace(hour=18, minute=59))
    st = Station(cfg, lambda: engine.FakeEngine(), data_dir=tmp_path, clock=clock)
    st.cat = CatControl(st, lambda c: Icom(radio, c["civ"], c["model"]))
    st.set_radio_setup("ic", {"antenna": "hf"})
    edge(st, clock, wed.replace(hour=19, minute=0))
    first = st.radios["ic"]["listen"]["now"]
    assert first in ("Local net", "160 m net")
    st.tx.append({"id": "t1", "radio": "ic", "status": "final", "text": "net control here",
                  "start": wed.replace(hour=19, minute=2).isoformat(timespec="seconds")})
    edge(st, clock, wed.replace(hour=19, minute=6))                  # 4 min since the last transmission: stays
    assert st.radios["ic"]["listen"]["now"] == first
    edge(st, clock, wed.replace(hour=19, minute=8))                  # 6 min silent (heard earlier): moves on
    L = st.radios["ic"]["listen"]
    assert L["now"] != first and L["switched"]["from"] == first
