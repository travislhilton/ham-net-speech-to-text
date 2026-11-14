"""Prepared topics, the operator's sites, and the operator's side chat (station/topics.py, station/chat.py)."""
import time
import urllib.error

import numpy as np
import pytest

from station import topics as TP
from tests.test_agent import T, _on, _say, _station, xai_key  # noqa: F401


def test_sites_are_parsed_and_named_in_order():
    s = TP.parse_sites("# comment\nCounty EOC | 39.80, -89.65 | 40 ft | EOC\nNorth Church | 39.85,-89.60 | 6 m\n"
                       "South Church | 39.70, -89.66")
    assert [x["name"] for x in s] == ["County EOC", "North Church", "South Church"]
    assert s[0]["height_m"] == pytest.approx(12.2, abs=0.1) and s[1]["height_m"] == 6 and s[2]["height_m"] == 6
    assert [x["name"] for x in TP.sites_named("path from south church to the county eoc?", s)] == \
        ["South Church", "County EOC"]
    with pytest.raises(ValueError, match="lat, lon"):
        TP.parse_sites("Bad | somewhere")


def test_path_facts_are_given_as_computed_numbers_and_failures_said():
    s = TP.parse_sites("A | 39.80, -89.65\nB | 39.85, -89.60\nC | 39.70, -89.66")
    calls = []

    def analyze(a, b):
        calls.append((a["name"], b["name"]))
        if b["name"] == "C" and a["name"] == "B":
            raise ConnectionError("offline")
        return {"text": f"{a['name']}-{b['name']}: clear line of sight, +30 dB VARA FM"}
    f = TP.path_facts(s, 146.0, analyze=analyze)
    assert "authoritative" in f[0] and len(calls) == 3
    assert any("A-B: clear" in x for x in f) and any("B to C: not computed (ConnectionError" in x for x in f)


def test_a_script_is_split_into_transmissions_at_sentence_ends():
    script = ("One two three four five. Six seven eight nine ten.\n\n" + "Word " * 30 + "end.\n\nShort one.")
    segs = TP.segment(script, 20)
    assert all(len(x.split()) <= 20 for x in segs)
    assert segs[0] == "One two three four five. Six seven eight nine ten."


def _topic_station(tmp_path):
    st = _station(tmp_path)
    _on(st)
    return st


def _prepared(st, n_paras=3):
    t = st.agent.topics.upsert({"request": "Explain how this station logs a net", "minutes": 2})
    script = "\n\n".join(f"Paragraph {i}. The station writes down every transmission." for i in range(n_paras))
    st.agent.topics.prepare(t["id"], lambda system, user: {"title": "Logging a net", "script": script,
                                                           "sources": ["[README.md › Install]"]},
                            None, {"call": "W9XYZ", "name": "Sidekick", "op": "Pat"})
    return st.agent.topics.get(t["id"])


def test_prepare_adds_hand_offs_and_the_id_and_an_edit_unapproves(tmp_path):
    st = _topic_station(tmp_path)
    t = _prepared(st)
    assert t["title"] == "Logging a net" and len(t["segments"]) >= 1
    assert t["segments"][-1].endswith("That concludes the topic. Back to net control. W9XYZ.")
    with pytest.raises(ValueError, match="approve"):
        st.agent.topics.arm("ic", t["id"])
    st.agent.topics.approve(t["id"])
    st.agent.topics.arm("ic", t["id"])
    st.agent.topics.upsert({"id": t["id"], "segments": [*t["segments"][:-1], "Changed words. W9XYZ."]})
    assert st.agent.topics.get(t["id"])["approved"] is None              # approval is bound to the words
    assert st.agent.topics.next_segment("ic") is None


def test_on_the_air_present_then_continue(tmp_path, xai_key):
    def down(key, model, system, user, **kw):
        raise urllib.error.URLError("no route")
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", down), "anthropic": ("ANTHROPIC_API_KEY", down)})
    t = st.agent.topics.upsert({"request": "r", "segments": ["Part one words. I'll pause there. W9XYZ.",
                                                             "Part two words. That concludes the topic. W9XYZ."]})
    st.agent.topics.approve(t["id"])
    st.agent.topics.arm("ic", t["id"])
    _say(st, "We'll take the training topic now.", speaker="K7QXN", minute=1)
    assert st.agent.draft("ic", "t0")["kind"] != "present"               # not asked to present
    _say(st, "W9XYZ Sidekick, please present the training topic.", calls=["W9XYZ"], speaker="K7QXN", minute=2)
    d = st.agent.draft("ic", "t1")
    assert d["kind"] == "present" and d["ok"], [c for c in d["fcc"] if not c["ok"]]
    assert "an automated assistant" in d["say"] and "Part one words" in d["say"]   # disclosed first
    _say(st, "W9XYZ, go ahead and continue.", calls=["W9XYZ"], speaker="K7QXN", minute=3)
    d2 = st.agent.draft("ic", "t2")
    assert d2["kind"] == "present" and d2["say"].startswith("Part two words") and d2["ok"]
    _say(st, "W9XYZ, go ahead.", calls=["W9XYZ"], speaker="K7QXN", minute=4)
    assert st.agent.draft("ic", "t3")["kind"] != "present"               # finished: nothing more to present


def test_chat_has_the_net_context_and_its_words_are_checked_saved_and_heard(tmp_path, xai_key):
    seen = {}

    def provider(key, model, system, user, **kw):
        seen["system"], seen["user"] = system, user
        return {"reply": "Use a J-pole at 30 ft.", "say": "This is W9XYZ, we recommend a J-pole at thirty feet. W9XYZ.",
                "title": "Antenna advice"}, {}
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", provider), "anthropic": ("ANTHROPIC_API_KEY", provider)})
    st.radios["ic"]["freq_mhz"] = 146.52
    _say(st, "K7ABC, Pat, Springfield, no traffic.", calls=["K7ABC"], speaker="K7ABC", minute=3)
    st.agent.chat.ask("ic", "What antenna should K7ABC use for the EOC?", wait=True)
    th = st.agent.chat.thread("ic")
    assert [m["role"] for m in th] == ["operator", "assistant"]
    a = th[-1]
    assert a["text"] == "Use a J-pole at 30 ft." and a["say"].endswith("W9XYZ.") and all(c["ok"] for c in a["check"])
    assert "Part 97" in seen["system"] and "146.52 MHz" in seen["user"] and "K7ABC, Pat, Springfield" in seen["user"]
    assert "Nothing you write here is transmitted" in seen["system"]
    v = st.agent.topics_post({"action": "save_say", "title": a["title"], "text": a["say"], "approve": True})
    item = v["topics"]["topics"][-1]
    assert item["title"] == "Antenna advice" and item["approved"] and item["segments"][-1].endswith("W9XYZ.")


def test_a_chat_answer_records_which_tools_it_used(tmp_path, xai_key, monkeypatch):
    from station import agent as A

    def tools(key, model, system, user, toolbox, **kw):
        assert "search_library" in system                              # the tools note is in the system prompt
        return {"reply": "The net meets at 7:30.", "say": "", "title": ""}, {
            "tools": ["search_library", "path_analysis", "search_library"]}
    monkeypatch.setattr(A, "call_xai_tools", tools)
    st = _station(tmp_path)
    _on(st, {"xai": ("XAI_API_KEY", A.call_xai), "anthropic": ("ANTHROPIC_API_KEY", A.call_anthropic)})
    st.agent.chat.ask("ic", "When does the net meet?", wait=True)
    a = st.agent.chat.thread("ic")[-1]
    assert a["text"] == "The net meets at 7:30." and a["tools"] == ["search_library", "path_analysis"]


def test_a_prepared_part_is_sent_by_the_operator_only_in_send_mode(tmp_path, xai_key, monkeypatch):
    from station import sender as S
    monkeypatch.setattr(S, "COUNTDOWN_S", 0.2)
    monkeypatch.setattr(S, "LEAD_S", 0.01)
    monkeypatch.setattr(S, "TAIL_S", 0.01)
    st = _station(tmp_path)
    _on(st)
    st.radios["ic"]["freq_mhz"] = 146.52
    st.radio_cfg["ic"]["tx"] = {"enabled": True, "ptt": "cm108", "hid": "h", "pin": 3, "out": "o"}
    keyed = []

    class P:
        def set(self, on): keyed.append(on)
        def close(self): keyed.append("close")
    st.ptt.factory = lambda t: P()
    st.agent.sender.voice_fn = lambda text: (np.zeros(2400, "float32"), 24000, "test")
    st.agent.sender.play_fn = lambda *a: None
    t = st.agent.topics.upsert({"request": "r", "segments": ["Net news part one. W9XYZ."]})
    with pytest.raises(ValueError, match="approve"):
        st.agent.set_radio("ic", "send")
        st.agent.topics_post({"action": "send", "radio": "ic", "id": t["id"], "seg": 0})
    st.agent.set_radio("ic", "draft")
    st.agent.topics.approve(t["id"])
    with pytest.raises(ValueError, match="Send first"):
        st.agent.topics_post({"action": "send", "radio": "ic", "id": t["id"], "seg": 0})
    st.agent.set_radio("ic", "send")
    r = st.agent.topics_post({"action": "send", "radio": "ic", "id": t["id"], "seg": 0})
    assert r["send"]["state"] == "counting"
    for _ in range(100):
        if st.agent.sender.out["ic"]["state"] == "sent":
            break
        time.sleep(0.02)
    assert st.agent.sender.out["ic"]["state"] == "sent" and keyed[0] is True
    assert st.agent.topics.armed["ic"]["next"] == 1
