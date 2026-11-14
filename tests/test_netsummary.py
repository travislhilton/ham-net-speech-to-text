"""The Summary at the top of a net's minutes (owner 2026-11-14): rule lines always; an AI paragraph by default when
the xAI key is there (station/netsummary.py), a setting turns it off; both go in the .md export and the ICS-213."""
import copy
import threading
import urllib.error
from datetime import datetime

import pytest

from station import engine, netsummary
from station.core import Station
from station.server import serve
from tests.net_fixtures import ARES, lines
from tests.test_history import CFG, TUESDAYS, _post, hist, write_logs

KEY = "ares@2026-11-03"


def test_summary_lines_lead_the_minutes(tmp_path):
    h = hist(tmp_path)
    for e in lines(ARES, TUESDAYS[-1], first_id=100):
        h.ingest(e)
    d = h.instance(KEY)
    s = d["summary"]
    assert s["ai"] is None and s["lines"]
    assert any("took part" in x and "net control" in x for x in s["lines"])
    assert any(x.startswith("Action items (") for x in s["lines"])
    md = h.minutes_text(KEY)
    assert md.index("## Summary") < md.index("## Attendance")
    txt = h.minutes_text(KEY, md=False)
    assert "SUMMARY" in txt and "  * " in txt


def _station(tmp_path, key=True):
    es = lines(ARES, TUESDAYS[-1], first_id=100)
    write_logs(tmp_path, es)
    if key:
        (tmp_path / "config").mkdir(exist_ok=True)
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path,
                 clock=lambda: datetime(2026, 11, 3, 21, 0))
    st.history.catch_up(tmp_path / "logs")
    return st, es


def test_the_ai_paragraph_is_written_once_kept_when_edited_and_rewritten_on_ask(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    monkeypatch.setattr(netsummary, "enabled", lambda s: True)
    asked = []
    st._summarizer = netsummary.Summarizer(st, call=lambda sys, user: asked.append(user) or
                                           f"The ARES net met; version {len(asked)}.")
    assert st.summarizer.due(datetime(2026, 11, 3, 21, 0)) == [KEY]
    assert st.summarizer.tick(datetime(2026, 11, 3, 21, 0)) == 1
    assert "K7NET" in asked[0] and "Transcript" in asked[0]          # told the roll and the words
    assert st.summarizer.tick(datetime(2026, 11, 3, 21, 0)) == 0     # unchanged: not asked again
    ai = st.history.instance(KEY)["summary"]["ai"]
    assert ai["text"].endswith("version 1.") and not ai["edited"]
    st.summary_post({"key": KEY, "action": "edit", "text": "My own words."})
    assert st.summarizer.due(datetime(2026, 11, 3, 21, 0)) == []     # an edit is never written over by itself
    d = st.summary_post({"key": KEY, "action": "rewrite"})
    assert d["summary"]["ai"]["text"].endswith("version 2.")
    md = st.history.minutes_text(KEY)
    assert "version 2." in md and "AI-drafted" in md and md.index("version 2.") < md.index("## Attendance")
    st.summary_post({"key": KEY, "action": "clear"})
    assert st.history.instance(KEY)["summary"]["ai"] is None


def test_the_setting_and_the_key_turn_it_off(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert not netsummary.enabled(st)                                 # no key: rule lines only
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    assert netsummary.enabled(st) and st.operator()["ai_summary"] is True   # on by default with a key
    st.set_operator({"callsign": "W9XYZ", "ai_summary": False})
    assert not netsummary.enabled(st) and st.operator()["ai_summary"] is False
    calls = []
    st._summarizer = netsummary.Summarizer(st, call=lambda s, u: calls.append(1) or "x")
    assert st.summarizer.tick(datetime(2026, 11, 3, 21, 0)) == 0 and not calls


def test_the_ics213_carries_the_summary_before_the_stations_heard(tmp_path, monkeypatch):
    st, es = _station(tmp_path)
    st.history.set_summary_ai(KEY, "The ARES net met and passed one message.", "test", "sig")
    st.tx = [dict(e) for e in es]
    pv = st.winlink_preview("ftm", es[0]["start"], es[-1]["start"])
    msg = next(d for d in pv["drafts"] if d["form"] == "ics213")["values"]["Message"]
    assert msg.startswith("Springfield ARES Net")
    assert msg.index("Summary:") < msg.index("passed one message") < msg.index("Stations heard:")
    assert "Action items (" in msg


def test_summary_api(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    st._summarizer = netsummary.Summarizer(st, call=lambda s, u: "Written over HTTP.")
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert _post(base, "/api/history/summary", {"key": KEY, "action": "rewrite"})["summary"]["ai"]["text"] == \
            "Written over HTTP."
        assert _post(base, "/api/history/summary", {"key": KEY, "action": "edit", "text": "Fixed."})["summary"]["ai"][
            "edited"]
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(base, "/api/history/summary", {"key": KEY, "action": "nope"})
        assert e.value.code == 400
    finally:
        srv.shutdown()


def test_calls_heard_once_are_not_counted_or_named_as_newcomers(tmp_path):
    h = hist(tmp_path)
    for e in lines(ARES, TUESDAYS[-1], first_id=100):
        h.ingest(e)
    d = h.instance(KEY)
    roll = [p for p in d["participants"] if p["role"] in ("ncs", "checkin")]
    sure = [p for p in roll if p["confirmed"]]
    assert len(sure) < len(roll)                                        # the fixture has calls heard once
    line = next(x for x in d["summary"]["lines"] if "took part" in x)
    assert line.startswith(f"{len(sure)} station") and "more heard once" in line
    p = netsummary.prompt(h, KEY, d)
    once = [x["call"] for x in roll if not x["confirmed"]]
    took = next(x for x in p.split("\n") if x.startswith("Took part"))
    assert not any(c in took for c in once) and "do not count or name them" in p
