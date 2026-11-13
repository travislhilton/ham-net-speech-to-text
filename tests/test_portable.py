"""Share settings between stations (station/portable.py): export, preview, apply."""
import copy
import json

import pytest

from station import portable as P
from tests.test_agent import CFG, _station


def _two(tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(); b_dir.mkdir()
    a = _station(a_dir)
    a.nets.add({"name": "Tuesday Net", "radio": "ic", "freq_mhz": 146.94, "mode": "FM", "start": "19:00",
                "duration_min": 60, "repeat": {"freq": "weekly", "weekdays": ["Tue"]}, "tone_hz": 100.0})
    a.nets.add({"name": "No Radio Net", "freq_mhz": 7.2, "mode": "LSB", "start": "07:00", "duration_min": 30,
                "repeat": {"freq": "weekly", "weekdays": ["Sat"]}})
    a.agent.settings({"license_class": "extra", "name": "Buddy", "checkin": "This is W9XYZ, Pat in Springfield",
                      "operator": "Pat", "speed": 1.1})
    b = _station(b_dir)
    return a, b


def test_export_has_no_keys_call_or_radios_and_identity_only_when_asked(tmp_path):
    a, _ = _two(tmp_path)
    d = P.export(a)
    txt = json.dumps(d)
    assert d["schema"] == P.SCHEMA and len(d["nets"]) == 4 and d["sidekick"]["license_class"] == "extra"   # 2 in the fixture + 2
    assert "Pat" not in txt and "Buddy" not in txt and not d["identity_included"]
    assert "W9XYZ" not in txt and "XAI" not in txt and "sound" not in txt.lower()
    assert "ic" in [n.get("radio") for n in d["nets"]] and d["radios"][0]["key"] == "ic"
    full = P.export(a, identity=True)
    assert full["sidekick"]["name"] == "Buddy" and full["sidekick"]["operator"] == "Pat"


def test_preview_changes_nothing_and_suggests_the_radio_by_name_or_band(tmp_path):
    a, b = _two(tmp_path)
    doc = P.export(a, identity=True)
    before = json.dumps(b.cfg.get("schedule")), json.dumps(b.agent.cfg, sort_keys=True)
    pv = P.preview(b, doc)
    assert pv["nets"]["add"] >= 2 and not pv["nets"]["invalid"]
    assert pv["radios"][0]["key"] == "ic" and pv["targets"]
    assert {c["field"] for c in pv["sidekick"]} >= {"license_class", "name", "operator", "checkin"}
    assert before == (json.dumps(b.cfg.get("schedule")), json.dumps(b.agent.cfg, sort_keys=True))


def test_apply_adds_updates_never_deletes_backs_up_and_maps_radios(tmp_path):
    a, b = _two(tmp_path)
    b.nets.add({"name": "Only Here", "freq_mhz": 3.9, "mode": "LSB", "start": "08:00", "duration_min": 30,
                "repeat": {"freq": "weekly", "weekdays": ["Sun"]}})
    b.nets.add({"name": "Tuesday Net", "radio": "ic", "freq_mhz": 146.00, "mode": "FM", "start": "19:00",
                "duration_min": 30, "repeat": {"freq": "weekly", "weekdays": ["Mon"]}})
    doc = P.export(a, identity=True)
    r = P.apply(b, doc, {"ic": "ic"})
    names = {n["name"]: n for n in b.cfg["schedule"]}
    assert {"Only Here", "Tuesday Net", "No Radio Net", "Example HF net"} <= set(names)          # nothing deleted
    assert r["added"] >= 1 and r["updated"] == 1 and names["Tuesday Net"]["freq_mhz"] == 146.94
    assert names["Tuesday Net"]["radio"] == "ic" and names["No Radio Net"].get("radio") is None
    assert b.agent.cfg["name"] == "Buddy" and b.agent.cfg["license_class"] == "extra"
    assert r["backup"] and (tmp_path / "b" / "settings_backups").exists()
    again = P.apply(b, doc, {"ic": "ic"})                                     # the same file again changes nothing
    assert again["added"] == 0 and again["updated"] == 0 and again["unchanged"] >= 2


def test_an_import_naming_no_radio_keeps_the_radio_a_net_already_has(tmp_path):
    a, b = _two(tmp_path)
    b.nets.add({"name": "Tuesday Net", "radio": "ic", "freq_mhz": 146.00, "mode": "FM", "start": "19:00",
                "duration_min": 30, "repeat": {"freq": "weekly", "weekdays": ["Mon"]}})
    P.apply(b, P.export(a), {"ic": ""})
    t = next(n for n in b.cfg["schedule"] if n["name"] == "Tuesday Net")
    assert t["radio"] == "ic" and t["freq_mhz"] == 146.94


def test_a_bad_file_or_bad_net_is_refused_or_skipped_not_half_applied(tmp_path):
    a, b = _two(tmp_path)
    with pytest.raises(ValueError, match="not a settings file"):
        P.preview(b, {"schema": "other/1"})
    doc = copy.deepcopy(P.export(a))
    doc["nets"].append({"name": "Broken", "start": "99:99", "duration_min": 5000, "mode": "FM"})
    pv = P.preview(b, doc)
    assert pv["nets"]["invalid"]
    r = P.apply(b, doc)
    assert r["added"] >= 2 and len(r["invalid"]) == 1 and not any(n["name"] == "Broken" for n in b.cfg["schedule"])


def test_sidekick_enabled_and_radio_modes_never_travel(tmp_path):
    a, b = _two(tmp_path)
    a.agent.settings({"enabled": True})
    d = P.export(a)
    assert "enabled" not in d["sidekick"] and "radios" not in d["sidekick"]
    d["sidekick"]["enabled"] = True
    d["sidekick"]["radios"] = {"ic": "send"}
    P.apply(b, d)
    assert not b.agent.cfg.get("enabled") and not (b.agent.cfg.get("radios") or {})
