"""Speculative drafting (agent.draft(speculate=True)): the model starts when a transmission is transcribed, not after the
DEBOUNCE_S of quiet, and its answer is used only for an identical prompt and context."""
import time

import pytest

from station import agent as A
from tests.test_agent import _on, _say, _station, xai_key  # noqa: F401

CHECKIN = {"speak": True, "kind": "check_in", "say": "W9XYZ, Echo, automated assistant, Pat is control operator.",
           "wait_s": 0.5, "why": "check-ins called"}


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(A, "DEBOUNCE_S", 0.4)


def _model(calls, delay=0.25):
    def fake(key, model, system, user, **kw):
        calls.append(time.monotonic())
        time.sleep(delay)
        return dict(CHECKIN), {}
    return {"xai": ("XAI_API_KEY", fake), "anthropic": ("ANTHROPIC_API_KEY", fake)}


def _drafts(st, n=1, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(st.agent.drafts) < n:
        time.sleep(0.02)
    return st.agent.drafts


def _hear(st, text, tid):
    _say(st, text, tid=tid)
    st.agent.on_final(st.tx[-1])


def test_the_model_starts_when_the_transmission_is_heard_and_its_answer_is_used(tmp_path, xai_key, fast):
    calls = []
    st = _station(tmp_path)
    _on(st, _model(calls))
    t0 = time.monotonic()
    _hear(st, "Any more check-ins for the net?", "t1")
    d = _drafts(st)[-1]
    assert len(calls) == 1 and calls[0] - t0 < 0.2                  # asked at once, not after the quiet
    assert d["kind"] == "check_in" and d["ok"] and d["speculative"] and d["after"] == "t1"
    assert d["silence_s"] == pytest.approx(0.5, abs=0.11)          # the model ran inside the courtesy wait


def test_a_newer_transmission_replaces_the_guess(tmp_path, xai_key, fast):
    calls = []
    st = _station(tmp_path)
    _on(st, _model(calls))
    _hear(st, "Any more check-ins for the net?", "t1")
    time.sleep(0.1)
    _hear(st, "Last call, any more check-ins for the net?", "t2")
    ds = _drafts(st)
    time.sleep(0.6)
    assert len(st.agent.drafts) == 1 and ds[-1]["after"] == "t2" and ds[-1]["speculative"]
    assert len(calls) == 2                                          # the first guess was asked, then dropped


def test_a_changed_prompt_asks_afresh(tmp_path, xai_key, fast):
    calls = []
    st = _station(tmp_path)
    _on(st, _model(calls, delay=0.05))
    _hear(st, "Any more check-ins for the net?", "t1")
    time.sleep(0.15)
    st.radios["ic"]["carrier"] = True                               # someone keyed: the context is not the same
    d = _drafts(st)[-1]
    assert len(calls) == 2 and not d.get("speculative")


def test_nothing_is_asked_early_when_the_station_would_stay_silent(tmp_path, xai_key, fast):
    calls = []
    st = _station(tmp_path)
    _on(st, _model(calls))
    _hear(st, "K7QXN, go ahead with your traffic.", "t1")
    d = _drafts(st)[-1]
    assert calls == [] and d["kind"] == "none"


def test_speculation_can_be_switched_off(tmp_path, xai_key, fast):
    calls = []
    st = _station(tmp_path)
    _on(st, _model(calls))
    st.agent.speculate = False
    t0 = time.monotonic()
    _hear(st, "Any more check-ins for the net?", "t1")
    d = _drafts(st)[-1]
    assert len(calls) == 1 and calls[0] - t0 >= 0.39 and not d.get("speculative")
