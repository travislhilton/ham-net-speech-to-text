"""The end-of-net review (✓ right / ✗ wrong / ✎ fix per call: the human truth the accuracy figures need) and the live
net board (who is in, who to recheck, traffic, regulars not heard, net control's 10-minute ID)."""
import json
from datetime import datetime, timedelta

import pytest

from tests.net_fixtures import checkin_net, lines
from tests.test_history import NET, TUESDAYS, hist

KEY = "ares@2026-11-03"
T = TUESDAYS[-1]


def net_with_a_once_heard_call(h, t=T):
    es = checkin_net(t, ["K7ABC", "KD9AAA"], first_id=100)
    es += lines([(4, "KE7PAS, mobile, with one piece of traffic for the net.", ["KE7PAS"], [])], t, first_id=200,
                ncs="K7NET")
    for e in es:
        h.ingest(e)


def roll(d):
    return {p["call"]: p for p in d["participants"] if p["role"] in ("ncs", "checkin")}


def test_a_call_heard_once_is_unconfirmed_and_marked_in_the_minutes(tmp_path):
    h = hist(tmp_path)
    net_with_a_once_heard_call(h)
    r = roll(h.instance(KEY))
    assert r["K7ABC"]["confirmed"] and r["K7NET"]["confirmed"]          # heard twice / net control
    assert r["KE7PAS"]["confirmed"] is False and r["KE7PAS"]["traffic"] is True
    assert r["K7ABC"]["traffic"] is False                                # "no traffic" is not traffic
    txt = h.minutes_text(KEY)
    assert "KE7PAS" in txt and "(?)" in txt and "heard once" in txt


def test_review_right_wrong_and_fix_change_the_attendance_and_are_logged(tmp_path):
    h = hist(tmp_path)
    net_with_a_once_heard_call(h)
    d = h.set_minutes(KEY, {"review": {"call": "KE7PAS", "verdict": "fix", "to": "KE7TAS"}})
    r = roll(d)
    assert "KE7PAS" not in r and r["KE7TAS"]["confirmed"] and r["KE7TAS"]["review"] == "ok"
    assert d["review"]["removed"] == [{"call": "KE7PAS", "mark": {"fix": "KE7TAS"}}]
    d = h.set_minutes(KEY, {"review": {"call": "KD9AAA", "verdict": "wrong"}})
    assert "KD9AAA" not in roll(d)
    d = h.set_minutes(KEY, {"review": {"call": "K7ABC", "verdict": "ok"}})
    assert roll(d)["K7ABC"]["review"] == "ok" and d["review"]["reviewed"] == 2   # K7ABC + KE7TAS
    d = h.set_minutes(KEY, {"review": {"call": "KE7PAS", "verdict": "undo"}})   # put back as heard
    assert "KE7PAS" in roll(d) and "KE7TAS" not in roll(d)
    log = [json.loads(x) for x in (tmp_path / "review_log.jsonl").read_text().splitlines()]
    assert [x["verdict"] for x in log] == ["fix", "wrong", "ok", "undo"]
    assert log[0] == log[0] | {"call": "KE7PAS", "to": "KE7TAS", "n": 1, "role": "checkin"}
    with pytest.raises(ValueError):
        h.set_minutes(KEY, {"review": {"call": "KE7PAS", "verdict": "maybe"}})
    with pytest.raises(ValueError):
        h.set_minutes(KEY, {"review": {"call": "KE7PAS", "verdict": "fix", "to": "not a call"}})


def test_the_review_survives_a_rebuild_of_the_index(tmp_path):
    h = hist(tmp_path)
    net_with_a_once_heard_call(h)
    h.set_minutes(KEY, {"review": {"call": "KE7PAS", "verdict": "ok"}})
    h.con.execute("UPDATE inst SET dirty=1")
    assert roll(h.instance(KEY))["KE7PAS"]["confirmed"]


def test_the_net_board_while_the_net_runs(tmp_path):
    h = hist(tmp_path, now=T + timedelta(minutes=16))
    for t in TUESDAYS[:-1]:                                     # four earlier nets: K7ABC and AE7QQ are regulars
        for e in checkin_net(t, ["K7ABC", "AE7QQ"], first_id=int(t.strftime("%d")) * 1000):
            h.ingest(e)
    net_with_a_once_heard_call(h)
    b = h.board("ftm", T + timedelta(minutes=16))
    assert b["key"] == KEY and b["ncs"] == "K7NET"
    assert [p["call"] for p in b["roll"]][:3] == ["K7NET", "K7ABC", "KD9AAA"]
    assert b["recheck"] == ["KE7PAS"] and b["traffic"] == ["KE7PAS"]
    assert b["missing"] == ["AE7QQ"]                            # a regular not heard yet tonight
    assert b["ncs_id_min"] == 16 and b["id_every_min"] == 10    # net control last said its call at the open
    assert h.board("ftm", T + timedelta(hours=3)) is None      # nothing on the air
