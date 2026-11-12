"""Sidekick's turn: who net control called, read the way the 2026-11-12 review of real nets found it misread
(phrasings from real nets; calls and places made up). Plus Sidekick with no internet and with no xAI key."""
import os
import urllib.error

import pytest

from station.agent import acknowledged, checkin_invited, rules_draft
from tests.test_agent import CTX_R, _on, _say, _station, xai_key  # noqa: F401  (fixture)


def inv(*texts, region="", gap_s=2.0):
    rows, t = [], 0.0
    for x in texts:
        rows.append({"text": x, "start": f"2026-11-10T20:00:{int(t):02d}", "duration_s": 3})
        t += 3 + gap_s
    return checkin_invited(rows, region)


OPEN = ["Any more check-ins for the net, please call now.",
        "Any stations wishing to check in, please call now.",
        "This is K7NET, net control. Any station, any where, check-ins please.",
        "Any additional check-ins? Please call now.",
        "We're open for check-ins, any station.",
        "Any late or missed check-ins?"]


@pytest.mark.parametrize("text", OPEN)
def test_open_calls_still_invite(text):
    assert inv(text)[0] is True, inv(text)


NOT_US = [
    ("Any station holding traffic, traffic only. Check in now.", "kind of station"),
    ("One more call for the maritime or marine mobiles…. Please call now.", "kind of station"),
    ("Any stations with traffic for the net, please check in now.", "kind of station"),
    ("Before I start taking check-ins, a few announcements. Check-ins will be after the bulletin.", "later"),
    ("I'll be taking check-ins in just a moment.", "later"),
    ("Please call now.", "not heard"),
    ("Any stations in Colorado, please call now.", "Colorado"),
    ("Wyoming stations, check in now please.", "Wyoming"),
]


@pytest.mark.parametrize("text,why", NOT_US)
def test_calls_for_someone_else_do_not_invite(text, why):
    ok, w = inv(text)
    assert ok is False and why.lower() in w.lower(), w


def test_a_region_call_for_this_stations_region_invites():
    assert inv("Any stations in Colorado, please call now.", region="Colorado, Larimer County")[0] is True
    assert inv("Any stations in Colorado, please call now.", region="Kansas")[0] is False


def test_the_restriction_in_the_transmission_just_before_counts():
    assert inv("This next call is for mobile stations only.", "Please call now with your call.")[0] is False
    assert inv("This next call is for mobile stations only.", "Please call now with your call.", gap_s=40)[0] is False


def test_net_control_acknowledging_this_station_is_not_a_question():
    assert acknowledged("W9XYZ, thank you, you're in the log.")
    assert acknowledged("Good evening W9XYZ, got you.")
    assert not acknowledged("W9XYZ, do you have any traffic?")
    assert not acknowledged("W9XYZ, go ahead.")


def test_an_acknowledgement_marks_checked_in_and_draws_no_reply(tmp_path, xai_key):
    asked = []
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", lambda *a, **k: asked.append(1)), "anthropic": ("ANTHROPIC_API_KEY", None)})
    _say(st, "W9XYZ, thank you, you're in the log.", calls=["W9XYZ"], speaker="K7NET")
    d = st.agent.draft("ic", "t0")
    assert d["speak"] is False and asked == []
    t = st.agent.turn("ic")
    assert t["checked_in"] and t["state"] == "in" and "net control acknowledged" in t["checked_in_how"]
    _say(st, "Any more check-ins for the net?", minute=5)
    assert st.agent.draft("ic", "t1")["speak"] is False                     # already in


def test_break_break_silences_sidekick_on_any_net(tmp_path, xai_key):
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked"))),
             "anthropic": ("ANTHROPIC_API_KEY", None)})
    _say(st, "Break break, emergency traffic. Any more check-ins?")
    d = st.agent.draft("ic", "t0")
    assert d["speak"] is False and "emergency" in d["why"]


def test_the_operator_marks_checked_in_and_mutes_for_this_net(tmp_path):
    st = _station(tmp_path)
    _on(st)
    st.agent.mark("ic", "checked_in")
    assert st.agent.turn("ic")["checked_in_how"] == "the operator checked in"
    _say(st, "Any more check-ins for the net?")
    assert st.agent.draft("ic", "t0")["speak"] is False
    st.agent.mark("ic", "not_checked_in"); st.agent.mark("ic", "mute")
    d = st.agent.draft("ic", "t0")
    assert d["speak"] is False and d["why"] == "muted for this net" and st.agent.turn("ic")["state"] == "muted"
    st.agent.mark("ic", "unmute")
    assert st.agent.draft("ic", "t0")["kind"] == "check_in"
    with pytest.raises(ValueError):
        st.agent.mark("ic", "transmit")


def test_a_net_set_to_never_check_in(tmp_path):
    st = _station(tmp_path)
    _on(st)
    st.agent._net = lambda rid, now, rows=(): {"name": "Mobile Net", "key": "m@1", "checkins": "none"}
    _say(st, "Any more check-ins for the net?")
    d = st.agent.draft("ic", "t0")
    assert d["speak"] is False and "never check in" in d["why"]


def test_the_offline_rules_read_the_same_turns():
    rows = [{"text": "Any station holding traffic, traffic only. Check in now."}]
    assert rules_draft(rows, CTX_R | {"invited": False}, checked_in=False)["speak"] is False


# ------------------------------------------------ no internet, no key ------------------------------------------
def test_no_xai_key_sidekick_says_it_uses_its_offline_rules_and_still_takes_turns(tmp_path):
    os.environ.pop("XAI_API_KEY", None)
    st = _station(tmp_path)
    _on(st)
    v = st.agent.view()
    assert v["mode_now"]["how"] == "rules" and "no xAI key" in v["mode_now"]["say"]
    assert all(not m["ready"] for m in v["models"] if m["id"].startswith("grok"))
    _say(st, "Any more check-ins for the net, please call now")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "rules" and d["kind"] == "check_in" and d["ok"] and "no XAI_API_KEY" in d["fallback"]
    _say(st, "W9XYZ, what's the weather like up there?", minute=5, calls=["W9XYZ"])
    d = st.agent.draft("ic", "t1")
    assert d["kind"] == "acknowledge" and "personally" in d["say"]           # no thinking without a model
    assert st.agent.view()["pending"] == {}
    _say(st, "Nice weather out there today.", minute=6)
    assert st.agent.draft("ic", "t2")["speak"] is False


def test_no_xai_key_hear_it_says_why_and_nothing_is_called(tmp_path):
    os.environ.pop("XAI_API_KEY", None)
    st = _station(tmp_path)
    st.tts.synth_fn = lambda *a, **k: (_ for _ in ()).throw(AssertionError("called xAI"))
    with pytest.raises(LookupError, match="xAI key"):
        st.tts.clip("W9XYZ checking in", "eve")


def test_no_internet_sidekick_keeps_drafting_from_rules_and_says_so(tmp_path, xai_key):
    def down(*a, **k):
        raise urllib.error.URLError("[Errno 65] No route to host")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", down), "anthropic": ("ANTHROPIC_API_KEY", down)})
    _say(st, "Any more check-ins for the net, please call now")
    d = st.agent.draft("ic", "t0")
    assert d["model"] == "rules" and d["kind"] == "check_in"
    m = st.agent.view()["mode_now"]
    assert m["how"] == "rules" and m["why"] == "offline" and "tries again" in m["say"]
    _say(st, "W9XYZ, can you look up the band plan for 17 meters?", minute=5, calls=["W9XYZ"])
    d = st.agent.draft("ic", "t1")
    assert d["model"] == "rules" and d["kind"] == "acknowledge" and st.agent.view()["pending"] == {}


def test_no_internet_hear_it_fails_fast_with_a_reason(tmp_path, xai_key):
    st = _station(tmp_path)
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise urllib.error.URLError("[Errno 65] No route to host")
    st.tts.synth_fn = down
    with pytest.raises(LookupError, match="unreachable"):
        st.tts.clip("W9XYZ checking in", "eve")
    with pytest.raises(LookupError, match="offline"):
        st.tts.clip("W9XYZ checking in again", "eve")                     # inside the back-off: not tried
    assert calls == [1]
