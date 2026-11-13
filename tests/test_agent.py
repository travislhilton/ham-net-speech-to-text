"""The station agent in Draft mode: FCC checks in code, timing, and that nothing is ever transmitted."""
import copy

import pytest
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

    def fake(key, model, system, user, **kw):
        seen["system"], seen["user"] = system, user
        return {"speak": True, "kind": "check_in", "say": "W9XYZ, Echo, automated assistant, Pat is control operator.",
                "wait_s": 2.0, "why": "check-ins called"}, {}
    st.agent.providers = {"xai": ("XAI_API_KEY", fake), "anthropic": ("ANTHROPIC_API_KEY", fake)}
    import os
    os.environ["XAI_API_KEY"] = "xai-" + "a" * 30
    try:
        assert st.agent.mode("ic") == "off"                      # off by default
        with pytest.raises(ValueError):
            st.agent.set_radio("ic", "draft")                    # the operator must enable the agent first
        st.agent.settings({"enabled": True, "operator": "Pat", "license_class": "extra",
                           "checkin": "This is W9XYZ, Pat in Springfield"})
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

    with pytest.raises(ValueError):
        st.agent.set_radio("ic", "transmit")                 # no mode that keys the radio exists
    from station import cat
    with pytest.raises(cat.CatError):
        cat.frame(0xA4, 0x1C, 0x00, b"\x01")                 # PTT is still refused before the port


# ---------------------------------------------------------------- Sidekick (11-10 evening) -------------------------
import json
import os
import threading
import time
import urllib.error

from station.agent import name_in, name_problem, rules_draft


def _station(tmp_path, hour=20, minute=5):
    return Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path,
                   clock=Clock(T.replace(hour=hour, minute=minute)))


def _say(st, text, minute=4, speaker="K7ABC", calls=(), net=None, tid=None):
    st.tx.append({"id": tid or f"t{len(st.tx)}", "radio": "ic", "status": "final",
                  "start": T.replace(hour=20, minute=minute).isoformat(), "text": text, "callsigns": list(calls),
                  "speaker": speaker, **({"net": net} if net else {})})


@pytest.fixture
def xai_key():
    os.environ["XAI_API_KEY"] = "xai-" + "a" * 30
    yield
    del os.environ["XAI_API_KEY"]


def _on(st, providers=None):
    if providers:
        st.agent.providers = providers
        st.agent.online = {}
    st.agent.settings({"enabled": True, "operator": "Pat", "license_class": "extra"})
    st.agent.set_radio("ic", "draft")


def test_the_default_name_is_sidekick_and_a_call_like_name_is_refused(tmp_path):
    st = _station(tmp_path)
    assert st.agent.name == "Sidekick" and st.agent.view()["default_name"] == "Sidekick"
    for bad_name in ("Echo", "Victor", "Lima Bean", "Portable", "Mayday", "Niner", "K9", "x"):
        assert name_problem(bad_name), bad_name
        with pytest.raises(ValueError):
            st.agent.settings({"name": bad_name})
    assert name_problem("W9 XYZ", "W9XYZ") and name_problem("Sidekick") is None
    assert st.agent.settings({"name": "Rosie"})["settings"]["name"] == "Rosie"      # the operator's own name
    assert st.agent.settings({"name": ""})["settings"]["name"] == "Sidekick"        # blank = the default


def test_a_saved_echo_becomes_sidekick_once_and_a_chosen_name_stays(tmp_path):
    (tmp_path / "agent.json").write_text(json.dumps({"name": "Echo", "operator": "Pat"}))
    assert _station(tmp_path).agent.name == "Sidekick"
    assert json.loads((tmp_path / "agent.json").read_text())["name"] == "Sidekick"
    other = tmp_path / "b"
    other.mkdir()
    (other / "agent.json").write_text(json.dumps({"name": "Rosie"}))
    assert _station(other).agent.name == "Rosie"


def test_the_name_is_heard_with_or_without_a_space():
    assert name_in("go ahead Side kick", "Sidekick") and name_in("SIDE-KICK, you there?", "Sidekick")
    assert not name_in("a sidekicker", "Sidekick") and not name_in("", "Sidekick")


CTX_R = {"call": "W9XYZ", "name": "Sidekick", "op": "Pat", "first": True, "id_due": True, "emergency": False,
         "busy": False, "freq_mhz": 3.915, "mode": "LSB", "license_class": "general", "addressed": False}


def test_the_offline_rules_check_in_acknowledge_and_otherwise_stay_silent():
    def rows(text):
        return [{"text": text}]
    d = rules_draft(rows("Any more check-ins for the net, please call now"), CTX_R, checked_in=False)
    assert d["kind"] == "check_in" and bad(d, CTX_R) == [] and "automated" in d["say"]
    assert rules_draft(rows("Any more check-ins?"), CTX_R, checked_in=True)["speak"] is False
    assert rules_draft(rows("Any maritime mobile stations for check-in?"), CTX_R, checked_in=False)["speak"] is False
    assert rules_draft(rows("Nice weather out there today."), CTX_R, checked_in=False)["speak"] is False
    a = rules_draft(rows("W9XYZ, are you there?"), CTX_R | {"addressed": True}, checked_in=True)
    assert a["kind"] == "acknowledge" and bad(a, CTX_R) == []
    later = CTX_R | {"addressed": True, "first": False, "id_due": False}
    assert bad(rules_draft(rows("Sidekick?"), later, checked_in=True), later) == []
    assert rules_draft(rows("any check-ins"), CTX_R | {"emergency": True}, False)["speak"] is False


def test_offline_the_rules_answer_at_once_and_the_model_waits_out_a_back_off(tmp_path, xai_key):
    tried = []

    def down(key, model, system, user, **kw):
        tried.append(model)
        raise urllib.error.URLError("no route to host")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", down), "anthropic": ("ANTHROPIC_API_KEY", down)})
    _say(st, "Any more check-ins for the net?")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "rules" and d["kind"] == "check_in" and d["ok"] and "unreachable" in d["fallback"]
    assert st.agent.view()["online"]["xai"]["online"] is False
    _say(st, "W9XYZ, go ahead with your traffic", calls=["W9XYZ"])
    d2 = st.agent.draft("ic", "t1")
    assert tried == ["grok-4.20-0309-non-reasoning"]          # not tried again inside the back-off
    assert d2["model"] == "rules" and d2["kind"] == "acknowledge" and "offline" in d2["fallback"]


def test_an_http_error_is_not_offline_and_no_key_uses_the_rules(tmp_path, xai_key):
    def refused(key, model, system, user, **kw):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", refused), "anthropic": ("ANTHROPIC_API_KEY", refused)})
    _say(st, "Any more check-ins?")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "rules" and "401" in d["fallback"] and st.agent.view()["online"]["xai"]["online"] is True
    del os.environ["XAI_API_KEY"]
    try:
        st2 = _station(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
        _on(st2)
        _say(st2, "Any more check-ins?")
        assert st2.agent.draft("ic", "t0")["model"] == "rules"
    finally:
        os.environ["XAI_API_KEY"] = "xai-" + "a" * 30


def test_a_restart_mid_net_does_not_check_in_twice(tmp_path, xai_key):
    net = {"key": "n1@2026-11-10", "id": "n1", "name": "Springfield Net"}

    def checkin(key, model, system, user, **kw):
        return {"speak": True, "kind": "check_in", "say": "W9XYZ, Sidekick, automated assistant, Pat is control "
                "operator, checking in.", "wait_s": 2, "why": "called"}, {}
    st = _station(tmp_path)
    st.cfg["schedule"] = []                                   # no scheduled net: the transmission's own net counts
    _on(st, {"xai": ("XAI_API_KEY", checkin), "anthropic": ("ANTHROPIC_API_KEY", checkin)})
    _say(st, "Any check-ins?", net=net)
    assert st.agent.draft("ic", "t0")["kind"] == "check_in"
    saved = json.loads((tmp_path / "agent_state.json").read_text())
    assert saved["ic"]["checked_in"] == "n1@2026-11-10"

    seen = {}

    def look(key, model, system, user, **kw):
        seen["user"] = user
        return {"speak": False, "kind": "none", "say": "", "wait_s": 2, "why": "done"}, {}
    st2 = _station(tmp_path)                                  # the station restarted
    st2.cfg["schedule"] = []
    st2.agent.providers = {"xai": ("XAI_API_KEY", look), "anthropic": ("ANTHROPIC_API_KEY", look)}
    _say(st2, "Any more check-ins?", net=net)
    quiet = st2.agent.draft("ic", "t0")
    assert quiet["model"] == "station" and "already checked in" in quiet["why"] and "user" not in seen  # no model
    st2.agent.draft("ic", "t0", asked=True)                   # the operator asks: the model sees the memory
    assert "W9XYZ has already checked in" in seen["user"] and "first transmission in this contact: no" in seen["user"]
    assert "Net: Springfield Net" in seen["user"]             # the net from the transmission when none is scheduled


def _two_speed(answer="W9XYZ... K7ABC, sixty meters is five channels, upper sideband, one hundred watts.", deep=None):
    calls = {"quick": [], "deep": []}

    def provider(key, model, system, user, **kw):
        if kw.get("schema") is agent.THINK_SCHEMA:
            calls["deep"].append(user)
            if deep:
                return deep(user)
            return {"answer": answer, "confident": True, "why": "band plan"}, {}
        calls["quick"].append(user)
        last = [x for x in user.splitlines() if x.startswith("[")][-1]
        none = {"speak": False, "kind": "none", "say": "", "wait_s": 2, "why": "-", "question": ""}
        if "what is the band plan" in last and "Your prepared answer" not in user:
            return {"speak": True, "kind": "working", "say": "W9XYZ, Sidekick, an automated assistant, Pat is "
                    "control operator, copy your question on sixty meters, working on it.", "wait_s": 1.5,
                    "why": "needs thinking", "question": "K7ABC asked for the 60 m band plan"}, {}
        if "READY for" in user and "Any comments" in last:
            return {"speak": True, "kind": "ready", "say": "This is W9XYZ Sidekick, I have a response ready for the "
                    "earlier inquiry from K7ABC.", "wait_s": 2, "why": "floor open", "question": ""}, {}
        if "ANNOUNCED" in user and "go ahead" in last:
            return {"speak": True, "kind": "answer", "say": "", "wait_s": 1, "why": "go ahead", "question": ""}, {}
        return none, {}
    return provider, calls


def _settle(st):
    th = getattr(st.agent, "_thinking", None)
    if th:
        th.join(5)
    for _ in range(50):
        t = st.agent.timers.get("ic")
        if not (t and t.is_alive()):
            break
        time.sleep(0.02)
    time.sleep(0.05)


def test_a_question_that_needs_thinking_is_answered_when_net_control_gives_the_floor(tmp_path, xai_key):
    provider, calls = _two_speed()
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", provider), "anthropic": ("ANTHROPIC_API_KEY", provider)})
    _say(st, "Sidekick, what is the band plan for sixty meters?", speaker="K7ABC", minute=1)
    d = st.agent.draft("ic", "t0")
    assert d["kind"] == "working" and d["ok"]
    _settle(st)
    p = st.agent.view()["pending"]["ic"]
    assert p["state"] == "ready" and p["asker"] == "K7ABC" and "sixty meters" in p["answer"]
    assert len(calls["deep"]) == 1 and "60 m band plan" in calls["deep"][0]
    assert st.agent.drafts[-1]["trigger"] == "answer ready" and not st.agent.drafts[-1]["speak"]  # no opening yet
    assert "READY for" in calls["quick"][-1]

    _say(st, "Any comments or questions for the net?", speaker="K7QXN", minute=2)
    r = st.agent.draft("ic", "t1")
    assert r["kind"] == "ready" and r["ok"] and st.agent.view()["pending"]["ic"]["state"] == "announced"
    _say(st, "Sidekick, go ahead", speaker="K7QXN", minute=3)
    a = st.agent.draft("ic", "t2")
    assert a["kind"] == "answer" and a["ok"], [c for c in a.get("fcc", []) if not c["ok"]]
    assert a["say"].startswith("W9XYZ... K7ABC, sixty meters")          # the prepared words, not a paraphrase
    assert "ic" not in st.agent.view()["pending"]                    # delivered


def test_ready_or_answer_without_a_prepared_answer_is_refused(tmp_path, xai_key):
    def quick(key, model, system, user, **kw):
        return {"speak": True, "kind": "ready", "say": "This is W9XYZ Sidekick, I have a response ready.",
                "wait_s": 2, "why": "x", "question": ""}, {}
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", quick), "anthropic": ("ANTHROPIC_API_KEY", quick)})
    _say(st, "Any comments for the net?")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "station" and not d["speak"]          # nothing ready: not offered, the model is not asked
    ctx = CTX_R | {"addressed": True}                        # and the code check behind it
    for kind in ("ready", "answer"):
        x = st.agent._protocol({"speak": True, "kind": kind, "say": "W9XYZ ready", "ok": True, "fcc": []}, None, ctx)
        assert not x["ok"] and x["fcc"][-1]["rule"] == "protocol"


def test_offline_the_rules_still_announce_and_read_a_prepared_answer(tmp_path, xai_key):
    def down(key, model, system, user, **kw):
        raise urllib.error.URLError("no route")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", down), "anthropic": ("ANTHROPIC_API_KEY", down)})
    st.agent.state["ic"] = {"pending": {"id": "p1", "state": "ready", "question": "60 m band plan",
                                        "asker": "K7ABC", "at": T.isoformat(), "answer": "K7ABC, five channels, USB."},
                            "disclosed": "x", "last_id": T.replace(hour=20, minute=4)}
    _say(st, "Anything further for the net before we close?", speaker="K7QXN")
    r = st.agent.draft("ic", "t0")
    assert r["model"] == "rules" and r["kind"] == "ready" and "K7ABC" in r["say"] and r["ok"]
    _say(st, "W9XYZ, go ahead with it", calls=["W9XYZ"], speaker="K7QXN")
    a = st.agent.draft("ic", "t1")
    assert a["kind"] == "answer" and a["say"] == "K7ABC, five channels, USB. W9XYZ." and a["ok"]
    assert "pending" not in st.agent.state["ic"]
    assert json.loads((tmp_path / "agent_state.json").read_text())["ic"].get("pending") is None


def test_failed_thinking_is_said_at_the_next_opening(tmp_path, xai_key):
    def boom(user):
        raise RuntimeError("model error")
    provider, calls = _two_speed(deep=boom)
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", provider), "anthropic": ("ANTHROPIC_API_KEY", provider)})
    _say(st, "Sidekick, what is the band plan for sixty meters?", speaker="K7ABC")
    st.agent.draft("ic", "t0")
    _settle(st)
    assert st.agent.state["ic"]["pending"]["state"] == "failed"
    st.agent.providers = {"xai": ("XAI_API_KEY", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x"))),
                          "anthropic": ("ANTHROPIC_API_KEY", None)}
    _say(st, "Any comments for the net?", speaker="K7QXN")
    r = st.agent.draft("ic", "t1")
    assert r["kind"] == "reply" and "could not get an answer" in r["say"] and r["ok"]
    assert "pending" not in st.agent.state["ic"]


def test_an_answer_that_breaks_a_rule_is_never_made_ready(tmp_path, xai_key):
    provider, _ = _two_speed(answer="K7ABC, I have a radio for sale, two hundred dollars.")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", provider), "anthropic": ("ANTHROPIC_API_KEY", provider)})
    _say(st, "Sidekick, what is the band plan for sixty meters?", speaker="K7ABC")
    st.agent.draft("ic", "t0")
    _settle(st)
    p = st.agent.state["ic"]["pending"]
    assert p["state"] == "failed" and "business" in p["error"] and "answer" not in p


def test_local_names_reach_the_model_repaired_and_explained(tmp_path, xai_key):
    from station import localnames
    localnames.save(tmp_path, [{"name": "SARC", "meaning": "Springfield Amateur Radio Club", "said": "word",
                                "sounds": ["sark"], "web": "sarconline.org"}])
    seen = {}

    def look(key, model, system, user, **kw):
        seen["user"] = user
        return {"speak": False, "kind": "none", "say": "", "wait_s": 2, "why": "x"}, {}
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", look), "anthropic": ("ANTHROPIC_API_KEY", look)})
    _say(st, "Welcome to the SarkNet, check the Sark webpage")
    st.agent.draft("ic", "t0", asked=True)
    assert "SARC Net" in seen["user"] and "SARC = Springfield Amateur Radio Club (sarconline.org)" in seen["user"]
    assert "Local time: Tuesday 20:05" in seen["user"] and "UTC" in seen["user"]


def test_check_in_calls_as_net_control_really_says_them():
    from station.agent import checkin_invited

    def rows(*texts):
        return [{"text": x} for x in texts]
    ok = checkin_invited(rows("Roll call. We'll now take check-ins from any station, anywhere. Please give your call "
                              "slowly. This is Net Control Station W9XYZ. First station, please."))
    assert ok[0]                                              # net control's own ID is not a restriction
    assert not checkin_invited(rows("Are there any new city stations or emergency operations centers that would like "
                                    "to check in this evening? Come now, please."))[0]
    assert not checkin_invited(rows("Anyone else, either from the state, city, or county, that would like to check "
                                    "in, come now, please."))[0]
    seq = rows("We'll now take check-ins from any station, anywhere.", "K7ABC, Pat in Springfield.",
               "Got you checked in. Next station, please.")
    assert checkin_invited(seq) == (True, "net control asked for the next station during open check-ins")
    assert not checkin_invited(rows("Good evening. Next station, please."))[0]   # no open check-ins: a roll call
    assert not checkin_invited(rows("Any maritime mobile stations for check-in?"))[0]
    assert not checkin_invited(rows("This is net control. Are there any stations with emergency, medical, or "
                                    "priority traffic? Over."))[0]               # a traffic call, not check-ins
    assert not checkin_invited(rows("Any relays for emergency or priority traffic?"))[0]
    assert checkin_invited(rows("Any stations wishing to check in, call now."))[0]


def test_a_check_in_during_a_roll_call_of_places_is_stopped(tmp_path, xai_key):
    def eager(key, model, system, user, **kw):
        return {"speak": True, "kind": "check_in", "say": "W9XYZ, Sidekick, an automated assistant for Pat, Pat is "
                "control operator. Checking in, no traffic.", "wait_s": 2, "why": "x", "question": ""}, {}
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", eager), "anthropic": ("ANTHROPIC_API_KEY", eager)})
    _say(st, "Got you, Riverside. Now calling Springfield, log in as well.")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "station" and not d["speak"]          # a roll call: check-in is not offered
    _say(st, "Strong signal as well. Thank you. Riverside City.", minute=5)        # a place, no "now calling"
    assert not st.agent.draft("ic", "t1")["speak"]
    x = st.agent._protocol({"speak": True, "kind": "check_in", "say": "W9XYZ checking in", "ok": True, "fcc": []},
                           None, CTX_R | {"invited": False, "roll_call": True})
    assert not x["ok"] and "roll call" in x["fcc"][-1]["note"]          # the code check behind the gate
    _say(st, "We'll now take check-ins from any station, anywhere.", minute=6)
    d2 = st.agent.draft("ic", "t2")
    assert d2["ok"] and d2["kind"] == "check_in" and d2["model"] != "station"
    _say(st, "Any visitors? Come now, please.", minute=7)
    st.agent.state["ic"].pop("checked_in", None)
    assert st.agent.draft("ic", "t3")["ok"]


def test_one_timeout_is_a_slow_answer_two_are_an_outage():
    from station.online import Online
    o = Online("x")
    o.failed(TimeoutError("slow"))
    assert o.should_try() and o.online is not False
    o.failed(urllib.error.URLError(TimeoutError("slow")))
    assert not o.should_try() and o.online is False
    o2 = Online("y")
    o2.failed(urllib.error.URLError("no route to host"))
    assert not o2.should_try()                                # a refused connection: at once


def test_a_reply_nobody_asked_for_is_stopped(tmp_path, xai_key):
    def chatty(key, model, system, user, **kw):
        return {"speak": True, "kind": "reply", "say": "W9XYZ, Sidekick, an automated assistant, Pat is control "
                "operator. Thanks for the net, sounds interesting. W9XYZ.", "wait_s": 2, "why": "x", "question": ""}, {}
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", chatty), "anthropic": ("ANTHROPIC_API_KEY", chatty)})
    _say(st, "This is K7ABC, 73 to you and have a good evening.")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "station" and not d["speak"] and "not called" in d["why"]
    x = st.agent._protocol({"speak": True, "kind": "reply", "say": "W9XYZ thanks", "ok": True, "fcc": []}, None,
                           CTX_R)
    assert not x["ok"] and "nobody called this station" in x["fcc"][-1]["note"]
    _say(st, "W9XYZ, thanks for checking in tonight", calls=["W9XYZ"], minute=5)
    assert st.agent.draft("ic", "t1")["ok"]


def test_a_ready_answer_waits_for_its_place():
    from station.agent import allowed_kinds
    p = {"state": "ready", "question": "q", "asker": "W9QXJ"}
    net = CTX_R | {"in_net": True}
    assert "ready" not in allowed_kinds(net, True, p, "Let's talk about antennas before winter.")[0]
    assert "ready" in allowed_kinds(net, True, p, "Any comments or questions for the net?")[0]
    assert "ready" in allowed_kinds(net | {"addressed": True}, True, p, "Sidekick, do you have that?")[0]
    # outside a net (owner 11-12) the answer itself is given at the pause, not announced first
    out = allowed_kinds(CTX_R | {"in_net": False, "trigger": "answer ready"}, True, p, "73")[0]
    assert "answer" in out and "ready" not in out


def test_the_thinking_model_gets_the_privileges_from_the_code():
    from station.agent import facts
    f = " ".join(facts())
    assert "General: 1.8-2, 3.8-4, 7.175-7.3, 14.225-14.35" in f and "30 m (10.1 MHz) has no phone" in f


def test_outside_a_net_the_ready_answer_is_given_at_a_pause_and_says_whom_it_is_for(tmp_path, xai_key):
    """Owner 11-12: in a conversation, the answer at the next lull (or a turn-over), not an announcement first."""
    def down(key, model, system, user, **kw):
        raise urllib.error.URLError("no route")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", down), "anthropic": ("ANTHROPIC_API_KEY", down)})
    st.agent._net = lambda *a, **k: {}                          # a conversation, no net on
    st.agent.state["ic"] = {"pending": {"id": "p1", "state": "ready", "question": "60 m band plan",
                                        "asker": "K7ABC", "at": T.isoformat(), "answer": "Five channels, USB."},
                            "disclosed": f"ic@{T:%Y-%m-%d}", "last_id": T.replace(hour=20, minute=4)}
    _say(st, "Yeah the antenna came down in the wind last week", speaker="K7ABC")
    quiet = st.agent.draft("ic", "t0")                          # mid-conversation: not yet
    assert not quiet["speak"]
    assert st.agent.timers["ic"].is_alive()                     # a lull check is waiting
    st.agent.timers["ic"].cancel()
    a = st.agent.draft("ic", None, why="pause")                 # nobody keyed for PAUSE_S
    assert a["kind"] == "answer" and a["ok"], [c for c in a.get("fcc", []) if not c["ok"]]
    assert a["say"].startswith("This is W9XYZ Sidekick, for K7ABC: Five channels, USB.")
    assert "pending" not in st.agent.state["ic"]


def test_a_turn_over_or_being_asked_gives_the_answer_outside_a_net(tmp_path, xai_key):
    from station.agent import allowed_kinds, GO_AHEAD
    p = {"state": "ready", "question": "q", "asker": "K7ABC"}
    conv = CTX_R | {"in_net": False}
    assert "answer" in allowed_kinds(conv, True, p, "That's all I had, back to you.")[0]
    assert "answer" not in allowed_kinds(conv, True, p, "So then I went to the swap meet and")[0]
    assert "answer" in allowed_kinds(conv | {"addressed": True}, True, p, "Sidekick, what did you find?")[0]
    for asked in ("W9XYZ Sidekick, what did you find?", "Sidekick, give us your answer", "Sidekick, are you ready?"):
        assert GO_AHEAD.search(asked), asked


def test_several_questions_wait_their_turn_in_order(tmp_path, xai_key):
    """Owner 11-12: anyone may ask it anything; more than one question can be waiting."""
    asked = []

    def provider(key, model, system, user, **kw):
        if kw.get("schema") is agent.THINK_SCHEMA:
            q = [x for x in user.splitlines() if x.startswith("The question to answer:")][0]
            return {"answer": "Answer to " + q.split(":", 1)[1].split("(asked")[0].strip(), "confident": True,
                    "why": "x"}, {}
        last = [x for x in user.splitlines() if x.startswith("[")][-1]
        if "Sidekick," in last and "?" in last:
            q = last.split("Sidekick,", 1)[1].strip()
            asked.append(q)
            return {"speak": True, "kind": "working", "say": "W9XYZ, Sidekick, an automated assistant, Pat is control "
                    "operator, copy, working on it.", "wait_s": 1.5,
                    "why": "x", "question": q}, {}
        return {"speak": False, "kind": "none", "say": "", "wait_s": 2, "why": "-", "question": ""}, {}
    st = _station(tmp_path, minute=3)                          # the conversation is still going on
    _on(st, {"xai": ("XAI_API_KEY", provider), "anthropic": ("ANTHROPIC_API_KEY", provider)})
    st.agent._net = lambda *a, **k: {}                          # a conversation: anyone asks, answers at the pauses
    for i, (who, q) in enumerate((("K7ABC", "how high should a VHF antenna be?"),
                                  ("N0CALL", "what is VARA FM?"),
                                  ("K7QXN", "who runs the gateway?"))):
        _say(st, f"Sidekick, {q}", speaker=who, minute=1 + i)
        d = st.agent.draft("ic", f"t{i}")                        # asked back to back, before any answer is ready
        assert d["kind"] == "working" and d["ok"], (q, [c for c in d.get("fcc", []) if not c["ok"]])
    for th in [t for t in threading.enumerate() if t.name != "MainThread" and "Thread" in t.name]:
        th.join(5)
    for t in list(st.agent.timers.values()):
        t.cancel()
    s = st.agent.state["ic"]
    assert s["pending"]["asker"] == "K7ABC" and [x["asker"] for x in s["queue"]] == ["N0CALL", "K7QXN"]
    assert all(x["state"] == "ready" for x in [s["pending"], *s["queue"]]), [(x["state"], x.get("error")) for x in [s["pending"], *s["queue"]]]
    _say(st, "Sidekick, what is a fourth question?", speaker="K7ABC", minute=5)   # the list is full
    full = st.agent.draft("ic", "t9")
    assert not full.get("ok", True) or full["kind"] != "working"
    for t in list(st.agent.timers.values()):
        t.cancel()
    _say(st, "OK, we'll hear from it in a bit.", speaker="K7ABC", minute=3)
    st.clock.t = st.clock.t.replace(second=10)                  # then nobody keys for 10 s: a pause
    a = st.agent.draft("ic", None, why="pause")
    assert a["kind"] == "answer" and "for K7ABC" in a["say"] and "how high" in a["say"]
    assert st.agent.state["ic"]["pending"]["asker"] == "N0CALL"          # the next one moves up


def test_the_first_answer_at_a_pause_carries_the_disclosure(tmp_path, xai_key):
    def down(key, model, system, user, **kw):
        raise urllib.error.URLError("no route")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", down), "anthropic": ("ANTHROPIC_API_KEY", down)})
    st.agent._net = lambda *a, **k: {}
    st.agent.state["ic"] = {"pending": {"id": "p1", "state": "ready", "question": "q", "asker": "K7ABC",
                                        "at": T.isoformat(), "answer": "Five channels, USB."}}
    _say(st, "Well, that's about it, over.", speaker="K7ABC")
    a = st.agent.draft("ic", "t0")
    assert a["kind"] == "answer" and a["ok"], [c for c in a["fcc"] if not c["ok"]]
    assert a["say"].startswith("This is W9XYZ, Sidekick, an automated assistant, Pat is control operator, for K7ABC:")
