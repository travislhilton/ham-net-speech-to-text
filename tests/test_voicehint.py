"""station/voicehint.py: Who?'s "sounds like" suggestion — same net only, a clear match only."""
import copy

import numpy as np

from station import voicehint as vh


def unit(*xs):
    v = np.zeros(8); v[:len(xs)] = xs
    return v / np.linalg.norm(v)


A, B, C = unit(1, 0, 0, .1), unit(0, 1, 0, .1), unit(0, 0, 1, .1)


def tx(i, start, call=None, v=None, net="n1", radio="r", dur=5.0):
    return {"id": str(i), "start": start, "radio": radio, "net": {"key": net} if net else None, "freq_mhz": 146.52,
            "duration_s": dur, "_call": call, "_v": v}


def run(target, items):
    return vh.suggest(target, items, lambda t: t["_v"], lambda t: t["_call"])


def test_suggests_the_clearly_closest_voice_in_the_same_net():
    items = [tx(1, "2026-11-07T19:00:00", "K7ABC", A), tx(2, "2026-11-07T19:01:00", "W7XYZ", B),
             tx(3, "2026-11-07T19:02:00", "N0QZZ", C)]
    t = tx(9, "2026-11-07T19:05:00", None, unit(1, 0, 0, .12))
    s = run(t, items + [t])
    assert s["call"] == "K7ABC" and s["id"] == "1" and s["gap"] >= vh.GAP


def test_no_suggestion_without_a_clear_lead_or_without_labels():
    items = [tx(1, "2026-11-07T19:00:00", "K7ABC", A), tx(2, "2026-11-07T19:01:00", "W7XYZ", B)]
    between = tx(9, "2026-11-07T19:05:00", None, unit(1, 1, 0, .1))
    assert "none" in run(between, items)                                  # equally like both: no suggestion
    unlabelled = [tx(1, "2026-11-07T19:00:00", None, A)]
    assert run(tx(9, "2026-11-07T19:05:00", None, A), unlabelled)["none"].startswith("nobody has stated")
    short = tx(9, "2026-11-07T19:05:00", None, None, dur=1.0)
    assert "too short" in run(short, items)["none"]


def test_only_the_same_net_counts():
    other_net = [tx(1, "2026-11-07T19:00:00", "K7ABC", A, net="n2"), tx(2, "2026-11-07T19:00:00", "W7XYZ", B, net="n2")]
    assert "none" in run(tx(9, "2026-11-07T19:05:00", None, A), other_net)
    # no scheduled net: same radio and frequency within 2 hours
    a = tx(1, "2026-11-07T19:00:00", "K7ABC", A, net=None)
    assert vh.same_net(tx(9, "2026-11-07T20:30:00", net=None), a)
    assert not vh.same_net(tx(9, "2026-11-07T21:30:00", net=None), a)
    assert not vh.same_net(tx(9, "2026-11-07T19:30:00", net=None, radio="other"), a)


def test_the_suggestion_offered_is_kept_with_the_operators_answer(tmp_path):
    from station import engine
    from station.core import Station
    from tests.test_station import CFG
    cfg = copy.deepcopy(CFG); cfg["voice_hints"] = False
    st = Station(cfg, lambda: engine.FakeEngine(), data_dir=tmp_path)
    st.tx.append({"id": "t1", "radio": cfg["radios"][0]["id"], "start": "2026-11-07T19:00:00", "status": "final",
                  "text": "Yeah, I can hear you.", "callsigns": []})
    assert "off" in st.voice_suggest("t1")["none"]
    st.set_speaker("t1", "W7XYZ", suggested="K7ABC")
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert logs and '"voice_suggested": "K7ABC"' in logs[0].read_text()
