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
