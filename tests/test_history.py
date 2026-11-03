"""Net history, search, analysis and minutes (station/history.py): built from the day logs, kept across restarts."""
import copy
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pytest

from station import engine
from station.core import Station
from station.history import History, fts_query, mark
from station.server import serve
from tests.net_fixtures import ARES, EMERGENCY, checkin_net, lines

NET = {"id": "ares", "name": "Springfield ARES Net", "radio": "ftm", "freq_mhz": 146.94, "mode": "FM",
       "start": "19:00", "duration_min": 60, "ncs": "K7NET", "repeat": {"freq": "weekly", "weekdays": ["Tue"]}}
CFG = {"callsign": "W9XYZ", "radios": [{"id": "ftm", "name": "FTM-150R", "device": "x", "gate": "carrier",
                                        "freq_mhz": 146.94}],
       "schedule": [NET], "port": 0, "bind": "127.0.0.1", "qrz_online": False}
TUESDAYS = [datetime(2026, 10, d, 19, 0) for d in (6, 13, 20, 27)] + [datetime(2026, 11, 3, 19, 0)]


def write_logs(data, entries):
    by_day = {}
    for e in entries:
        by_day.setdefault((e.get("start") or e["_day"])[:10].replace("-", ""), []).append(e)
    (data / "logs").mkdir(parents=True, exist_ok=True)
    for day, es in by_day.items():
        with (data / "logs" / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            for e in es:
                f.write(json.dumps({k: v for k, v in e.items() if k != "_day"}) + "\n")


def hist(tmp_path, nets=(NET,), now=datetime(2026, 11, 3, 21, 0)):
    return History(tmp_path, nets=lambda: list(nets), clock=lambda: now, me="W9XYZ")


def five_weeks():
    rosters = [["K7ABC", "W9XYZ", "KD9AAA"], ["K7ABC", "W9XYZ"], ["K7ABC", "KD9AAA", "N0CALL"], ["K7ABC", "W9XYZ"],
               ["K7ABC", "AE7QQ", "N0CALL"]]
    out = []
    for i, (t, r) in enumerate(zip(TUESDAYS, rosters)):
        out += checkin_net(t, r, ncs="K7NET" if i != 3 else "K7ABC", first_id=10000 + i * 100)
    return out


def test_instances_from_the_schedule_and_conversations(tmp_path):
    h = hist(tmp_path)
    t = TUESDAYS[-1]
    for e in lines([(-20, "Anyone around for a chat? W9XYZ.", ["W9XYZ"], [])], t, first_id=1) + \
            lines(ARES, t, first_id=100) + \
            lines([(70, "K7ABC, one more thing after the net, W9XYZ.", ["K7ABC", "W9XYZ"], [])], t, first_id=200) + \
            lines([(150, "W9XYZ testing, W9XYZ.", ["W9XYZ"], [])], t, first_id=300):
        h.ingest(e)
    got = {i["key"]: i for i in h.instances()}
    assert set(got) == {"ftm@2026-11-03T18:40:00", "ares@2026-11-03", "ftm@2026-11-03T21:30:00"}
    net = got["ares@2026-11-03"]
    # the chat 20 min before is not the net; 10 minutes after its end is (a net running over stays that net)
    assert net["kind"] == "net" and net["name"] == "Springfield ARES Net" and net["tx_count"] == len(ARES) + 1
    assert net["start"] == "2026-11-03T19:00:00" and net["end"] == "2026-11-03T20:10:00" and net["ncs"] == "K7NET"
    d = h.instance("ares@2026-11-03")
    roll = [(p["call"], p["role"]) for p in d["participants"] if p["role"] != "mentioned"]
    assert roll[:4] == [("K7NET", "ncs"), ("K7ABC", "checkin"), ("W9XYZ", "checkin"), ("KD9AAA", "checkin")]
    assert ("W7TST", "checkin") in roll      # "N0CALL, I have one formal message": not stated, only mentioned
    kinds = sorted(i["kind"] for i in d["items"])
    assert kinds == ["action", "action", "action", "announcement", "announcement", "announcement", "decision",
                     "event", "traffic"]


def test_backfill_from_day_logs_with_updates_and_restart(tmp_path):
    es = lines(ARES, TUESDAYS[-1], first_id=100)
    upd = {"update": "108", "accurate": {"text": "K7NET, roger. K7ABC will email the volunteer roster by Friday.",
                                         "callsigns": ["K7NET", "K7ABC"], "stage": "final", "model": "large-v3"},
           "_day": "2026-11-03"}
    who = {"update": "102", "speaker_set": "W9XYZ", "_day": "2026-11-03"}
    write_logs(tmp_path, es + [upd, who] + five_weeks()[:20])
    h = hist(tmp_path)
    n = h.catch_up(tmp_path / "logs")
    assert n == len(es) + 2 + 20
    tx = h.instance("ares@2026-11-03")["tx"]
    assert next(t for t in tx if t["id"] == "108")["text"].startswith("K7NET, roger. K7ABC will email")
    assert next(t for t in tx if t["id"] == "108")["live_text"].startswith("K7NET, roger. K7ABC will send")
    assert next(t for t in tx if t["id"] == "102")["speaker"] == "W9XYZ"            # the operator's "Who?"
    assert h.catch_up(tmp_path / "logs") == 0                                       # nothing new: nothing re-read
    h.close()
    again = hist(tmp_path)                                                          # survives a restart
    assert again.instance("ares@2026-11-03")["tx_count"] == len(ARES)
    with (tmp_path / "logs" / "20261103.jsonl").open("a") as f:                     # only the new line is read
        f.write(json.dumps(lines([(30, "W7TST, late check-in, no traffic.", ["W7TST"], [])], TUESDAYS[-1],
                                 first_id=900)[0]) + "\n")
    assert again.catch_up(tmp_path / "logs") == 1
    # a fresh index (deleted history.db) is rebuilt the same from the logs
    again.close()
    (tmp_path / "history.db").unlink()
    fresh = hist(tmp_path)
    fresh.catch_up(tmp_path / "logs")
    assert fresh.instance("ares@2026-11-03")["tx_count"] == len(ARES) + 1


def test_search_everything(tmp_path):
    h = hist(tmp_path)
    for e in lines(ARES, TUESDAYS[-1], first_id=100) + lines(EMERGENCY, TUESDAYS[-2] + timedelta(hours=-2),
                                                             first_id=400):
        h.ingest(e)
    h.note_station({"call": "K7ABC", "name": "PAT EXAMPLE", "city": "SPRINGFIELD", "state": "IL"})
    r = h.search("hamfest")
    assert r["tx_total"] == 1 and r["tx"][0]["net_name"] == "Springfield ARES Net"
    assert "\x02hamfest\x03" in r["tx"][0]["snippet"].lower()
    assert [i["kind"] for i in r["items"]] == ["announcement"] and r["nets"] == []
    assert h.search("mile marker")["tx"][0]["id"] == "401"                          # the conversation (no net)
    assert h.search('"water is rising"')["tx_total"] == 1 and h.search('"rising water"')["tx_total"] == 0
    assert {s["call"] for s in h.search("pat")["stations"]} == {"K7ABC"}            # a callbook name
    assert h.search("K7A")["stations"][0]["call"] == "K7ABC"                        # a callsign prefix
    assert h.search("springfield ares")["nets"][0]["name"] == "Springfield ARES Net"
    assert h.search("", call="KD9AAA")["tx_total"] == 4                             # a callsign filter alone
    assert h.search("net", net="ares", frm="2026-11-03", to="2026-11-03")["tx_total"] == \
        h.search("net", net="ares")["tx_total"] > 0
    assert h.search("net", to="2026-10-01")["tx_total"] == 0
    # the same answers without FTS5 (plain LIKE), for a Python whose SQLite lacks it
    h.fts = False
    assert h.search("hamfest")["tx_total"] == 1 and "\x02" in h.search("hamfest")["tx"][0]["snippet"]
    assert [i["kind"] for i in h.search("hamfest")["items"]] == ["announcement"]
    assert fts_query('fire "mile marker" K7-ABC') == '"fire"* "mile marker" "K7"* "ABC"*'
    assert mark("A structure fire at Main", "fire") == "A structure \x02fire\x03 at Main"


def test_search_is_fast_at_100k_transmissions(tmp_path):
    h = hist(tmp_path)
    words = "check in no traffic roger thank you log weather repeater antenna net control hamfest meeting".split()
    rows = []
    t0 = datetime(2025, 1, 1)
    for i in range(100_000):
        txt = " ".join(words[(i * 7 + k) % len(words)] for k in range(8)) + f" K{i % 900}ABC"
        rows.append((str(i), "ftm", (t0 + timedelta(minutes=i)).isoformat(timespec="seconds"), txt, txt,
                     f" K{i % 900}ABC ", "ftm@x"))
    with h.lock:
        h.con.execute("BEGIN")
        h.con.executemany("INSERT INTO tx(id, radio, start, text, live_text, calls, inst) VALUES(?,?,?,?,?,?,?)", rows)
        h.con.execute("INSERT INTO tx_fts(rowid, text, live, calls) SELECT rowid, text, live_text, calls FROM tx")
        h.con.execute("COMMIT")
    t = time.perf_counter()
    r = h.search("hamfest meeting")
    common = time.perf_counter() - t
    t = time.perf_counter()
    r2 = h.search("K17ABC")
    rare = time.perf_counter() - t
    assert r["tx_total"] > 10_000 and len(r["tx"]) == 50 and r2["tx_total"] > 100
    print(f"\nsearch over 100k transmissions: common words {common * 1000:.0f} ms, a callsign {rare * 1000:.0f} ms")
    assert common < 2.0 and rare < 0.5, (common, rare)


def test_analysis_regulars_newcomers_rota(tmp_path):
    h = hist(tmp_path)
    for e in five_weeks():
        h.ingest(e)
    a = h.analysis("ares")
    assert a["held"] == 5 and "at least half of the last 8" in a["rule"]
    s = {r["call"]: r for r in a["stations"]}
    assert s["K7ABC"]["times"] == 5 and s["K7ABC"]["streak"] == 5 and s["K7ABC"]["first"] == "2026-10-06"
    assert s["W9XYZ"]["times"] == 3 and s["W9XYZ"]["streak"] == 0 and s["W9XYZ"]["last"] == "2026-10-27"
    assert set(a["regulars"]) == {"K7ABC", "W9XYZ", "K7NET"}           # 5/5, 3/5, net control 4/5 (KD9AAA 2/5: no)
    assert a["newcomers"] == ["AE7QQ"] and a["missing"] == ["W9XYZ"]
    assert [x["count"] for x in a["attendance"]] == [4, 3, 4, 2, 4]
    assert a["rota"][0] == {"call": "K7NET", "times": 4, "last": "2026-11-03"} and a["rota"][1]["call"] == "K7ABC"
    assert [n["name"] for n in h.station_nets("AE7QQ")] == ["Springfield ARES Net"]
    assert h.analysis("nope")["held"] == 0


def test_operator_layer_survives_re_extraction_and_carries_forward(tmp_path):
    h = hist(tmp_path)
    for e in lines(ARES, TUESDAYS[-2], first_id=100):
        h.ingest(e)
    d = h.instance("ares@2026-10-27")
    roster = next(i for i in d["items"] if "roster" in i["text"])
    ve = next(i for i in d["items"] if i["kind"] == "action" and "register" in i["text"])
    h.update_item(roster["id"], {"who": "KD9AAA", "due": "2026-11-06"})              # the operator's edit
    h.delete_item(ve["id"])                                                            # dismissed, not deleted
    added = h.add_item({"inst": "ares@2026-10-27", "kind": "action", "text": "Order new coax", "who": "W9XYZ"})
    # the accurate pass rewrites the transmission: drafts follow it, edits stay
    h.ingest({"update": "108", "accurate": {"text": "K7NET, roger. K7ABC will mail the volunteer roster to all by Friday.",
                                            "callsigns": ["K7NET", "K7ABC"], "stage": "final"}})
    h.ingest({"update": "107", "accurate": {"text": "VE testing is on the 21st, please register by November 18th.",
                                            "callsigns": [], "stage": "final"}})
    d = h.instance("ares@2026-10-27")
    r2 = next(i for i in d["items"] if i["id"] == roster["id"])
    assert r2["who"] == "KD9AAA" and r2["edited"] and "send the volunteer roster" in r2["text"]
    assert not [i for i in d["items"] if i["kind"] == "action" and "register" in i["text"]]   # stays dismissed
    assert any(i["id"] == added["id"] for i in d["items"])
    # next week: the open actions are carried forward and in "still open"
    for e in lines([(0, "This is K7NET, net control.", ["K7NET"], [])], TUESDAYS[-1], first_id=300):
        h.ingest(e)
    nxt = h.instance("ares@2026-11-03")
    assert {i["text"] for i in nxt["carried"]} >= {"Order new coax", r2["text"]}
    assert nxt["prev"] == "ares@2026-10-27" and h.instance("ares@2026-10-27")["next"] == "ares@2026-11-03"
    so = h.still_open("ares", datetime(2026, 11, 3, 18, 55))
    # still valid on 3 Nov: the hamfest (14 Nov) and VE testing; the club meeting (Thu 29 Oct) is past
    assert {i["text"][:20] for i in so["announcements"]} == {"Reminder, the Spring", "VE testing is on the"}
    assert so["upcoming"][0]["event_at"] == "2026-11-14T08:00" and len(so["actions"]) >= 3 and so["traffic"]
    h.update_item(added["id"], {"status": "done"})
    assert h.open_counts("ares", datetime(2026, 11, 3, 18, 55))["actions"] == len(so["actions"]) - 1
    # after the hamfest it is no longer read out
    late = h.still_open("ares", datetime(2026, 11, 20, 18, 0))
    assert not [i for i in late["announcements"] if "hamfest" in i["text"]]
    with pytest.raises(ValueError):
        h.update_item(added["id"], {"status": "maybe"})
    with pytest.raises(LookupError):
        h.add_item({"inst": "nope", "text": "x"})


def test_minutes_export_attendance_edits_and_urgent(tmp_path):
    h = hist(tmp_path, nets=(NET | {"emergency": True},))
    for e in lines(EMERGENCY, TUESDAYS[-1], first_id=400):
        h.ingest(e)
    d = h.set_minutes("ares@2026-11-03", {"notes": "County EOC activated at 19:05.", "add": "KC9ZZZ", "remove": "W7TST",
                                          "ncs": "k7net"})
    calls = {p["call"]: p for p in d["participants"]}
    assert calls["KC9ZZZ"]["source"] == "operator" and "W7TST" not in calls and d["ncs"] == "K7NET"
    assert d["emergency"] and len([i for i in d["items"] if i["kind"] == "urgent"]) == 7
    md = h.minutes_text("ares@2026-11-03")
    assert md.startswith("# Springfield ARES Net — 2026-11-03") and "## Urgent" in md and "EMERGENCY" in md
    assert "County EOC activated" in md and "KC9ZZZ" in md and "## Action items" in md
    assert "Urgent:" in h.summary_for_213("ares@2026-11-03")
    u = h.urgent_open()
    assert len(u) == 7 and u[0]["radio"] == "ftm"
    h.update_item(u[0]["id"], {"status": "handled"})
    assert len(h.urgent_open()) == 6


def _get(base, path):
    return json.loads(urllib.request.urlopen(base + path).read())


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def test_station_api_and_winlink_minutes(tmp_path):
    es = lines(ARES, TUESDAYS[-1], first_id=100)
    write_logs(tmp_path, es)
    (tmp_path / "recordings" / "20261103").mkdir(parents=True)
    (tmp_path / "recordings" / "20261103" / "100_ftm.wav").write_bytes(b"RIFF....WAVE")
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path,
                 clock=lambda: datetime(2026, 11, 3, 21, 0))
    st.history.catch_up(tmp_path / "logs")
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    key = urllib.parse.quote("ares@2026-11-03")
    try:
        assert _get(base, "/api/search?q=roster")["tx"][0]["id"] == "108"
        assert _get(base, "/api/history")["instances"][0]["key"] == "ares@2026-11-03"
        inst = _get(base, f"/api/history/instance?key={key}")
        assert inst["name"] == "Springfield ARES Net" and len(inst["tx"]) == len(ARES)
        assert _get(base, "/api/nets/analysis?net=ares")["held"] == 1
        assert _get(base, "/api/nets/open?net=ares")["upcoming"]
        assert _get(base, "/api/station/nets?call=K7ABC")["nets"][0]["times"] == 1
        it = _post(base, "/api/items", {"inst": "ares@2026-11-03", "kind": "decision", "text": "Buy a duplexer"})
        assert _post(base, f"/api/items/{it['id']}", {"status": "done"})["status"] == "done"
        assert _post(base, f"/api/items/{it['id']}/delete", {}) == {"deleted": it["id"]}
        assert _post(base, "/api/history/minutes", {"key": "ares@2026-11-03", "notes": "ok"})["notes"] == "ok"
        r = urllib.request.urlopen(base + f"/api/history/minutes?key={key}")
        assert r.headers["Content-Type"].startswith("text/markdown") and b"## Attendance" in r.read()
        assert urllib.request.urlopen(base + "/audio/100").read() == b"RIFF....WAVE"     # an old day's recording
        for path in (f"/api/history/instance?key=nope", "/audio/999"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path)
            assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(base, "/api/items/99999", {"status": "done"})
        assert e.value.code == 404
        # the Winlink summary carries the net's name and its minutes
        st.tx = [dict(e) for e in es]
        pv = st.winlink_preview("ftm", es[0]["start"], es[-1]["start"])
        msg = next(d for d in pv["drafts"] if d["form"] == "ics213")["values"]["Message"]
        assert pv["inst"] == "ares@2026-11-03" and msg.startswith("Springfield ARES Net") and "Action items:" in msg
    finally:
        srv.shutdown()
