"""Sidekick's Send mode (station/sender.py): a draft is said on the air only under the control operator's eye."""
import time
import urllib.error

import numpy as np
import pytest

from station import sender as S
from tests.test_agent import T, _on, _say, _station, xai_key  # noqa: F401  (fixture)


class FakePTT:
    def __init__(self):
        self.events = []

    def set(self, on):
        self.events.append(on)

    def close(self):
        self.events.append("close")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(S, "COUNTDOWN_S", 0.3)
    monkeypatch.setattr(S, "LEAD_S", 0.01)
    monkeypatch.setattr(S, "TAIL_S", 0.01)


def _tx_station(tmp_path, enabled=True):
    st = _station(tmp_path)
    st.radio_cfg["ic"]["tx"] = {"enabled": enabled, "ptt": "cm108", "hid": "hid-1", "pin": 3, "out": "Radio out"}
    dev = FakePTT()
    st.ptt.factory = lambda t: dev
    played = []
    st.agent.sender.voice_fn = lambda text: (np.zeros(2400, "float32"), 24000, "test")
    st.agent.sender.play_fn = lambda rid, pcm, sr, ev: played.append((rid, len(pcm), sr))
    return st, dev, played


def _down(key, model, system, user, **kw):
    raise urllib.error.URLError("no route")


def _speak(st):
    """A call to this station by name: the offline rules acknowledge it (a draft that speaks)."""
    _say(st, "Sidekick, are you on frequency?", speaker="K7ABC")
    st.radios["ic"]["freq_mhz"] = 146.52
    return st.agent.draft("ic", "t0")


def _wait(st, rid="ic", states=("sent", "stopped", "held", "failed")):
    for _ in range(100):
        o = st.agent.sender.out.get(rid)
        if o and o["state"] in states:
            return o
        time.sleep(0.02)
    return st.agent.sender.out.get(rid)


def test_send_needs_the_radios_transmit_switch(tmp_path):
    st, _, _ = _tx_station(tmp_path, enabled=False)
    _on(st)
    with pytest.raises(ValueError, match="transmit is switched off"):
        st.agent.set_radio("ic", "send")
    st.radio_cfg["ic"]["tx"]["enabled"] = True
    assert st.agent.set_radio("ic", "send")["settings"]["radios"]["ic"] == "send"


def test_away_from_the_station_it_is_held_and_nothing_keys(tmp_path, xai_key):
    st, dev, played = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    d = _speak(st)
    assert d["speak"] and d["ok"] and d["send"]["state"] == "held" and "not at the station" in d["send"]["why"]
    assert dev.events == [] and played == []
    assert "last_id" not in st.agent.state["ic"]                 # nothing was said: the ID clock did not move


def test_present_it_counts_down_keys_plays_and_lets_go(tmp_path, xai_key):
    st, dev, played = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    st.agent.sender.presence()
    d = _speak(st)
    assert d["send"]["state"] == "counting"
    assert "last_id" not in st.agent.state["ic"]                 # not yet said
    o = _wait(st)
    assert o["state"] == "sent", o
    assert dev.events == [True, False, "close"] or dev.events[:1] == [True]
    assert played == [("ic", 2400, 24000)]
    assert st.agent.state["ic"].get("last_id")                   # said on the air: the ID clock moves now
    assert (tmp_path / "agent_sends.jsonl").read_text().count('"sent"') == 1


def test_esc_during_the_countdown_cancels_before_key_up(tmp_path, xai_key):
    st, dev, played = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    st.agent.sender.presence()
    _speak(st)
    st.agent.post("/api/agent/stop/all", {})
    o = _wait(st)
    assert o["state"] == "stopped" and dev.events == [] and played == []


def test_someone_keying_first_takes_the_floor(tmp_path, xai_key):
    st, dev, played = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    st.agent.sender.presence()
    _speak(st)
    st.radios["ic"]["carrier"] = True                            # a signal on the frequency (listen before talk)
    o = _wait(st)
    assert o["state"] == "stopped" and "floor is theirs" in o["why"] and dev.events == []


def test_a_new_transmission_heard_cancels_the_countdown(tmp_path, xai_key):
    st, dev, _ = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    st.agent.sender.presence()
    _speak(st)
    st.agent.on_final({"radio": "ic", "id": "t9", "text": "K7QXN, go ahead", "speaker": "K7QXN"})
    o = _wait(st)
    assert o["state"] == "stopped" and dev.events == []
    for t in list(st.agent.timers.values()):
        t.cancel()


def test_stop_on_the_air_drops_ptt_at_once(tmp_path, xai_key):
    st, dev, _ = _tx_station(tmp_path)

    def slow_play(rid, pcm, sr, ev):
        ev.wait(5)                                               # a long transmission, stopped mid-way
    st.agent.sender.play_fn = slow_play
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    st.agent.sender.presence()
    _speak(st)
    _wait(st, states=("on_air",))
    t = time.monotonic()
    st.agent.sender.stop("ic")
    o = _wait(st)
    assert o["state"] == "stopped" and time.monotonic() - t < 1.0
    assert dev.events[0] is True and dev.events[-1] == "close"   # keyed, then let go
    assert "last_id" not in st.agent.state["ic"]                 # cut off: not counted as said


def test_unknown_frequency_is_never_sent(tmp_path, xai_key):
    st, dev, _ = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.set_radio("ic", "send")
    st.agent.sender.presence()
    _say(st, "Sidekick, are you on frequency?", speaker="K7ABC")
    st.radios["ic"]["freq_mhz"] = None
    d = st.agent.draft("ic", "t0")
    assert d["send"]["state"] == "held" and "frequency is not known" in d["send"]["why"] and dev.events == []


def test_draft_mode_never_keys(tmp_path, xai_key):
    st, dev, played = _tx_station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", _down), "anthropic": ("ANTHROPIC_API_KEY", _down)})
    st.agent.sender.presence()
    d = _speak(st)
    time.sleep(0.5)
    assert "send" not in d and dev.events == [] and played == []
