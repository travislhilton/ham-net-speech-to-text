"""Nets with no radio on this station, given radios a group at a time (station/netassign.py), and the hint an import
leaves on them (station/portable.py, from_radio)."""
import pytest

from station import netassign as NA
from station import portable as P
from tests.test_portable import _two


def _names(st):
    return {n["name"]: n for n in st.cfg["schedule"]}


def test_an_import_without_a_radio_keeps_a_hint_and_the_hint_suggests_the_radio(tmp_path):
    a, b = _two(tmp_path)
    P.apply(b, P.export(a), {"ic": ""})                        # the operator chose no radio for the IC-705's nets
    t = _names(b)["Tuesday Net"]
    assert t.get("radio") is None and t["from_radio"] == {"name": "IC-705"}
    g = NA.groups(b)
    hinted = next(x for x in g["groups"] if x["key"].startswith("from:"))
    assert hinted["label"] == "IC-705 at the other station" and hinted["suggest"] == "ic"   # same name here
    assert [n["name"] for n in hinted["nets"]] == ["Tuesday Net"]


def test_a_net_without_a_hint_is_grouped_by_band_and_mode_and_suggested_the_radio_doing_that_band(tmp_path):
    a, b = _two(tmp_path)
    P.apply(b, P.export(a), {"ic": "ic"})
    g = NA.groups(b)
    assert g["waiting"] == 1
    (only,) = g["groups"]
    assert only["label"] == "HF LSB nets" and [n["name"] for n in only["nets"]] == ["No Radio Net"]
    assert only["suggest"] == "ic"                              # the IC-705 already records an HF LSB net here


def test_assign_gives_the_chosen_radios_drops_the_hint_backs_up_and_leaves_the_rest(tmp_path):
    a, b = _two(tmp_path)
    P.apply(b, P.export(a), {"ic": ""})
    g = NA.groups(b)
    keys = {x["key"]: x for x in g["groups"]}
    hint_key = next(k for k in keys if k.startswith("from:"))
    other = next(k for k in keys if k != hint_key)
    before = len(b.cfg["schedule"])
    r = NA.assign(b, {hint_key: "ic", other: ""})
    n = _names(b)
    assert r["assigned"] == 1 and not r["failed"] and r["backup"]
    assert n["Tuesday Net"]["radio"] == "ic" and "from_radio" not in n["Tuesday Net"]
    assert n["Tuesday Net"]["freq_mhz"] == 146.94 and n["Tuesday Net"]["tone_hz"] == 100.0   # nothing else changed
    assert n["No Radio Net"].get("radio") is None and len(b.cfg["schedule"]) == before      # left, nothing deleted
    assert r["waiting"] == 1 and (b.data / "schedule.json").read_text().count("Tuesday Net") == 1


def test_a_radio_that_is_not_here_is_refused(tmp_path):
    a, b = _two(tmp_path)
    P.apply(b, P.export(a), {"ic": ""})
    with pytest.raises(ValueError):
        NA.assign(b, {NA.groups(b)["groups"][0]["key"]: "nope"})


def test_a_net_naming_a_radio_this_pc_does_not_have_is_waiting_too(tmp_path):
    a, _ = _two(tmp_path)
    nets = [dict(x) for x in a.cfg["schedule"]]
    next(x for x in nets if x["name"] == "Tuesday Net")["radio"] = "gone"
    a.cfg["schedule"] = nets
    g = NA.groups(a)
    names = [n["name"] for x in g["groups"] for n in x["nets"]]
    assert "Tuesday Net" in names and next(n for x in g["groups"] for n in x["nets"]
                                           if n["name"] == "Tuesday Net")["was"] == "gone"


def test_a_waiting_net_passes_its_hint_on_when_exported_again(tmp_path):
    a, b = _two(tmp_path)
    P.apply(b, P.export(a), {"ic": ""})
    doc = P.export(b)
    t = next(n for n in doc["nets"] if n["name"] == "Tuesday Net")
    assert "radio" not in t and t["from_radio"]["name"] == "IC-705"
    c_dir = tmp_path / "c"
    c_dir.mkdir()
    from tests.test_agent import _station
    c = _station(c_dir)
    c.radios["ic"]["name"] = "FT-991A"                         # a different radio here: no name match
    P.apply(c, doc, {})
    assert _names(c)["Tuesday Net"]["from_radio"]["name"] == "IC-705"


def test_with_no_name_match_the_suggestion_comes_from_the_nets_own_band(tmp_path):
    a, b = _two(tmp_path)
    b.radios["ic"]["name"] = "FT-991A"
    P.apply(b, P.export(a), {"ic": ""})
    hinted = next(x for x in NA.groups(b)["groups"] if x["key"].startswith("from:"))
    assert hinted["suggest"] is None                          # a VHF FM net; nothing here records or is tuned to VHF FM
    b.radios["ic"].update(freq_mhz=146.52, mode="FM")
    hinted = next(x for x in NA.groups(b)["groups"] if x["key"].startswith("from:"))
    assert hinted["suggest"] == "ic"                          # the only radio tuned to VHF FM
