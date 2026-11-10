"""The incident read-out (contracts/emcomm-readout): the contract fixtures, the template, the privacy rule, approval
bound to the fingerprint, and the log."""
import copy
import json
from pathlib import Path

import pytest

from station import engine
from station.core import Station
from station.readout import check, clock_words, fingerprint, script, words
from tests.test_cat import CFG, Clock, TUE as T

FIX = Path(__file__).resolve().parent.parent / "contracts" / "emcomm-readout" / "fixtures"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def refp(d):
    d["fingerprint"] = fingerprint(d)
    return d


def test_catalog_items_said_as_people_say_them():
    from station.readout import item_name
    assert item_name("Blanket—3-lb. poly fleece (10 per case)") == "Blanket, three-pound poly fleece"
    assert item_name("Bottled water (24 bottles per case)") == "Bottled water"
    assert item_name("Extension cord, 100'") == "Extension cord, one hundred foot"
    assert item_name("Tarp, 12 by 16 feet") == "Tarp, twelve by sixteen feet"


def test_numbers_and_times_in_words():
    assert words(41) == "forty-one" and words(0) == "zero" and words(74.5) == "seventy-four and a half"
    assert words(1250) == "one thousand two hundred fifty" and words(None) == "not reported"
    assert clock_words("2026-11-10T18:57:00-07:00") == "eighteen fifty-seven"
    assert clock_words("2026-11-10T19:00:00-07:00") == "nineteen hundred"
    assert clock_words("2026-11-10T09:05:00-07:00") == "zero nine zero five"


@pytest.mark.parametrize("name", ["typical", "exercise-nulls", "area"])
def test_every_contract_fixture_is_accepted_and_scripted(name):
    d = check(fx(name))
    sc = script(d, "W9XYZ", "Whiskey Nine X-ray Yankee Zulu")
    text = " ".join(s["text"] for s in sc)
    assert sc[0]["id"] == "callup" and sc[-1]["id"] == "close" and "back to net control" in sc[-1]["text"]
    assert all("Whiskey Nine X-ray Yankee Zulu" in s["text"] for s in sc)            # ID in every section
    assert all(s["seconds"] <= 45 for s in sc)
    assert not any(ch.isdigit() for ch in text)                                       # every number in words
    if d["source"]["event"]["exercise"]:
        assert all(s["text"].count("This is an exercise") >= (1 if s["id"] == "callup" else 2) for s in sc)


def test_the_typical_script_says_the_facts():
    t = " ".join(s["text"] for s in script(fx("typical"), "W9XYZ", "W9XYZ"))
    assert "Homes damaged: forty-one" in t and "Reports from four of six wards" in t
    assert "Larkspur fourth Ward and Larkspur sixth Ward" in t and "E-O-C" in t
    assert "Water, one gallon, two hundred" in t
    assert "PACE level Contingency, radio" in t and "Next report at twenty-three hundred local" in t


def test_missing_is_not_reported_never_zero():
    t = " ".join(s["text"] for s in script(fx("exercise-nulls"), "W9XYZ", "W9XYZ"))
    assert "Homes destroyed: not reported" in t and "Volunteers: not reported" in t


def test_a_new_event_with_no_reports_reads_cleanly():
    # first real EOC fetch: no EOC name, as_of null (nothing reported yet) -> "the the E-O-C ... as of not reported"
    d = copy.deepcopy(fx("typical"))
    d["source"]["eoc"]["name"] = None
    d["as_of"], d["generated_at"] = None, "2026-11-10T14:19:57-07:00"
    t = " ".join(s["text"] for s in script(d, "W9XYZ", "W9XYZ"))
    assert "the the" not in t and "the E-O-C situation report" in t
    assert "as of fourteen nineteen local" in t and "not reported local" not in t


def test_injuries_missionaries_and_people_are_refused():
    for key, val in (("injured", 3), ("missionaries", {"ok": 5}), ("submitter_phone", "x")):
        d = fx("typical")
        d["reporting"][key] = val
        with pytest.raises(ValueError, match="never read"):
            check(refp(d))
    d = fx("typical")
    d["notes"] = ["Two members injured near the bridge."]
    with pytest.raises(ValueError, match="injuries or missionaries"):
        script(check(refp(d)), "W9XYZ", "W9XYZ")


def test_a_changed_report_fails_its_fingerprint_and_a_new_major_is_named():
    d = fx("typical")
    d["property"]["homes_damaged"] = 40
    with pytest.raises(ValueError, match="fingerprint"):
        check(d)
    d = fx("typical")
    d["schema"] = "emcomm-readout/2"
    with pytest.raises(ValueError, match="newer report format"):
        check(d)


def test_approval_binds_to_the_fingerprint_and_new_facts_clear_it(tmp_path):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T.replace(hour=19)))
    ro = st.readout
    v = ro.load(fx("typical"), "test")
    assert v["role"] == "Stake President or his delegate" and v["approval"] is None
    with pytest.raises(ValueError):
        ro.mark({"section": "bottom"})                                   # not approved: nothing to read
    with pytest.raises(ValueError):
        ro.approve({"by": "Pat", "fingerprint": v["doc"]["fingerprint"]})  # must confirm the role
    v = ro.approve({"by": "Pat", "confirm_role": True, "fingerprint": v["doc"]["fingerprint"]})
    assert v["approval"]["by"] == "Pat"
    ro.mark({"section": "bottom"}); ro.mark({"section": "close"})
    assert (tmp_path / "readouts.jsonl").read_text().count('"read"') == 1
    d = fx("typical"); d["property"]["homes_damaged"] = 44
    v = ro.load(refp(d), "test")
    assert v["approval"] is None and "Homes damaged / destroyed: 41 / 2 → 44 / 2" in v["revoked"]["changes"]
    assert ro.load(fx("area"), "t")["role"] == "Area Authority or his delegate"


def test_fetch_from_the_eoc(tmp_path):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T))
    body = (FIX / "typical.json").read_bytes()

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n=-1): return body
    v = st.readout.fetch("http://127.0.0.1:8080/api/readout", opener=lambda req, timeout: R())
    assert v["doc"]["source"]["eoc"]["name"] == "Larkspur Stake EOC" and len(v["script"]) == 6
