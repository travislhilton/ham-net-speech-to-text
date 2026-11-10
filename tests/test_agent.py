"""The station agent in Draft mode: FCC checks in code, timing, and that nothing is ever transmitted."""
import copy
from datetime import timedelta

from station import agent, engine
from station.agent import fcc_check, phonetic, privileges
from station.core import Station
from tests.test_cat import CFG, Clock, TUE as T

CTX = {"call": "W9XYZ", "name": "Echo", "op": "Pat", "first": True, "id_due": True, "emergency": False,
       "busy": False, "freq_mhz": 3.915, "mode": "LSB", "license_class": "general"}


def bad(d, ctx=CTX):
    return [c["rule"] for c in fcc_check(d, ctx) if not c["ok"]]


def test_a_good_first_check_in_passes_every_rule():
    d = {"speak": True, "kind": "check_in", "say": "W9XYZ, Echo, the automated assistant for this station, "
         "Pat is control operator, checking in with no traffic."}
    assert bad(d) == []


def test_identification_and_disclosure_are_enforced():
    assert "97.119(a)" in bad({"speak": True, "kind": "reply", "say": "Thanks for the report, automated here."})
    assert "97.113(a)(4) disclosure" in bad({"speak": True, "kind": "check_in", "say": "W9XYZ checking in."})
    assert "97.113(a)(4) disclosure" in bad({"speak": True, "kind": "check_in", "say": "W9XYZ, automated assistant."})
    later = CTX | {"first": False, "id_due": False}
    assert bad({"speak": True, "kind": "acknowledge", "say": "Roger, thanks."}, later) == []
    assert "97.119(a)" in bad({"speak": True, "kind": "acknowledge", "say": "Roger, thanks."}, later | {"id_due": True})
    assert "97.119(a)" not in bad({"speak": True, "kind": "reply", "say": "Automated, Whiskey Nine X-ray Yankee Zulu"})


def test_content_rules():
    base = "W9XYZ automated assistant: "
    assert "97.113(a)(4) not a person" in bad({"speak": True, "kind": "reply", "say": base + "I'm a real person."})
    assert "97.113(a)(4) not a person" in bad({"speak": True, "kind": "reply", "say": base + "this is Pat speaking."})
    assert "97.113(a)(2),(3) business" in bad({"speak": True, "kind": "reply", "say": base + "I have one for sale."})
    assert "97.115 third party" in bad({"speak": True, "kind": "reply", "say": base + "I'm relaying a message for him."})
    assert "97.113(a)(4) codes" in bad({"speak": True, "kind": "reply", "say": base + "QXRTB ZZKLM PLMKO"})
    assert "97.101(d) interference" in bad({"speak": True, "kind": "reply", "say": base + "go ahead"}, CTX | {"busy": True})
    em = CTX | {"emergency": True}
    assert "97.101(c) emergency priority" in bad({"speak": True, "kind": "reply", "say": base + "hello"}, em)
    assert "97.101(c) emergency priority" not in bad({"speak": True, "kind": "reply", "say": base + "hello",
                                                      "addressed": True}, em)
    assert "transmit cap" in bad({"speak": True, "kind": "reply", "say": base + "word " * 80})
    assert bad({"speak": False, "kind": "none", "say": ""}) == []


def test_privileges():
    assert privileges(3.915, "LSB", "general")[0] is True
    assert privileges(3.700, "LSB", "general")[0] is False and privileges(3.700, "LSB", "extra")[0] is True
    assert privileges(14.200, "USB", "general")[0] is False
    assert privileges(7.200, "LSB", "technician")[0] is False and privileges(146.52, "FM", "technician")[0] is True
    assert privileges(5.3305, "USB", "general")[0] is True and privileges(5.3200, "USB", "general")[0] is False
    assert privileges(3.915, "LSB", "")[0] is None


def test_phonetic():
    assert phonetic("W9XYZ") == "Whiskey Nine X-ray Yankee Zulu"


def test_a_draft_runs_the_model_checks_it_and_never_transmits(tmp_path):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T.replace(hour=20, minute=5)))
    seen = {}

    def fake(key, model, system, user):
        seen["system"], seen["user"] = system, user
        return {"speak": True, "kind": "check_in", "say": "W9XYZ, Echo, automated assistant, Pat is control operator.",
                "wait_s": 2.0, "why": "check-ins called"}, {}
    st.agent.providers = {"xai": ("XAI_API_KEY", fake), "anthropic": ("ANTHROPIC_API_KEY", fake)}
    import os
    os.environ["XAI_API_KEY"] = "xai-" + "a" * 30
    try:
        st.agent.settings({"operator": "Pat", "license_class": "extra", "checkin": "This is W9XYZ, Pat in Springfield"})
        st.agent.set_radio("ic", "draft")
        st.tx.append({"id": "t1", "radio": "ic", "status": "final", "start": T.replace(hour=20, minute=4).isoformat(),
                      "text": "Any more check-ins for the net?", "callsigns": [], "speaker": "K7ABC"})
        d = st.agent.draft("ic", "t1")
    finally:
        del os.environ["XAI_API_KEY"]
    assert d["ok"] and d["speak"] and d["kind"] == "check_in"
    assert "Pat in Springfield" in seen["user"] and "automated voice assistant" in seen["system"]
    assert d["silence_s"] >= 1.2
    assert (tmp_path / "agent_drafts.jsonl").exists()
    import pytest
    with pytest.raises(ValueError):
        st.agent.set_radio("ic", "transmit")                 # no mode that keys the radio exists
    from station import cat
    with pytest.raises(cat.CatError):
        cat.frame(0xA4, 0x1C, 0x00, b"\x01")                 # PTT is still refused before the port
