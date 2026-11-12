"""Recordings and net history kept 6 weeks (station/retention.py)."""
import json
import os
import time
from datetime import date, datetime

from station import retention
from station.history import History

TODAY = date(2026, 12, 31)          # 6 weeks back: the first day kept is 2026-11-19


def tx(i, start, inst_text="net"):
    return {"id": str(i), "radio": "r1", "start": start, "status": "final", "text": f"{inst_text} {i}",
            "duration_s": 3.0, "callsigns": [], "wav": f"recordings/{start[:10].replace('-', '')}/{i}_r1.wav"}


def setup(tmp_path):
    d = tmp_path
    for day in ("20261117", "20261118", "20261119", "20261231"):
        (d / "recordings" / day).mkdir(parents=True)
        (d / "recordings" / day / "1_r1.wav").write_bytes(b"x" * 1000)
    (d / "logs").mkdir()
    rows = [tx(1, "2026-11-17T20:00:00"), tx(2, "2026-11-18T23:59:00"), tx(3, "2026-11-19T00:05:00"),
            tx(4, "2026-12-31T08:00:00")]
    for r in rows:
        f = d / "logs" / f"{r['start'][:10].replace('-', '')}.jsonl"
        with f.open("a") as fh:
            fh.write(json.dumps(r) + "\n")
    h = History(d)
    h.catch_up(d / "logs")
    return d, h


def test_older_than_six_weeks_goes_and_the_rest_stays(tmp_path):
    d, h = setup(tmp_path)
    old = time.mktime(datetime(2026, 11, 1).timetuple())
    for sub in ("voice", "grok_stt"):
        (d / sub).mkdir()
        for name, t in (("old.npy", old), ("new.npy", time.mktime(datetime(2026, 12, 30).timetuple()))):
            (d / sub / name).write_bytes(b"v")
            os.utime(d / sub / name, (t, t))
    (d / "agent_drafts.jsonl").write_text(json.dumps({"at": "2026-11-01T10:00:00"}) + "\n" +
                                          json.dumps({"at": "2026-12-30T10:00:00"}) + "\n")
    before = {r["id"] for r in h._q("SELECT id FROM tx")}
    assert before == {"1", "2", "3", "4"}
    dry = retention.sweep(d, 6, history=h, dry_run=True, today=TODAY)
    assert dry["recordings"]["n"] == 2 and (d / "recordings" / "20261117").exists()
    res = retention.sweep(d, 6, history=h, today=TODAY)
    assert res["keep_from"] == "2026-11-19"
    assert sorted(p.name for p in (d / "recordings").iterdir()) == ["20261119", "20261231"]
    assert sorted(p.name for p in (d / "logs").iterdir()) == ["20261119.jsonl", "20261231.jsonl"]
    left = {r["id"] for r in h._q("SELECT id FROM tx")}
    assert "1" not in left and "4" in left
    assert not h._q("SELECT k FROM meta WHERE k='off:20261117.jsonl'")
    assert [p.name for p in (d / "voice").iterdir()] == ["new.npy"] and [p.name for p in (d / "grok_stt").iterdir()] == ["new.npy"]
    assert (d / "agent_drafts.jsonl").read_text().count("\n") == 1
    if h.fts:
        assert not h._q("SELECT rowid FROM tx_fts WHERE tx_fts MATCH 'net' AND rowid NOT IN (SELECT rowid FROM tx)")
    assert "deleted recordings 2" in retention.say(res)
    again = retention.sweep(d, 6, history=h, today=TODAY)
    assert retention.say(again).endswith("nothing older")
    h.catch_up(d / "logs")                                   # a restart does not bring them back
    assert {r["id"] for r in h._q("SELECT id FROM tx")} == left


def test_a_net_that_started_before_the_cutoff_goes_whole(tmp_path):
    d, h = setup(tmp_path)
    # 23:59 on the 18th and 00:05 on the 19th: one conversation, which started before the first day kept
    assert h._one("SELECT inst FROM tx WHERE id='3'")["inst"] == h._one("SELECT inst FROM tx WHERE id='2'")["inst"]
    retention.sweep(d, 6, history=h, today=TODAY)
    assert not h._q("SELECT id FROM tx WHERE id IN ('2', '3')") and h._q("SELECT id FROM tx WHERE id='4'")
    assert not h._q("SELECT key FROM inst WHERE start < '2026-11-19'")


def test_raw_from_a_removed_radio_and_a_big_compile_cache(tmp_path, monkeypatch):
    now = time.time()
    raw = tmp_path / "raw" / "gone-radio"
    raw.mkdir(parents=True)
    (raw / "a.wav").write_bytes(b"r")
    os.utime(raw / "a.wav", (now - 3 * 86400, now - 3 * 86400))
    live = tmp_path / "raw" / "live"
    live.mkdir()
    (live / "b.wav").write_bytes(b"r")
    ov = tmp_path / "ov-cache"
    ov.mkdir()
    for i, age in enumerate((10, 9, 1)):                      # days since last used
        f = ov / f"{i}.blob"
        f.write_bytes(b"x" * 100)
        os.utime(f, (now - age * 86400, now - age * 86400))
    monkeypatch.setattr(retention, "OV_CAP", 150)
    res = retention.sweep(tmp_path, 6, today=TODAY, now=now)
    assert not (tmp_path / "raw" / "gone-radio").exists() and (live / "b.wav").exists()
    assert sorted(p.name for p in ov.iterdir()) == ["2.blob"] and res["OpenVINO cache"]["n"] == 2


def test_a_big_station_log_is_cut_to_its_tail(tmp_path):
    p = tmp_path / "station.log"
    p.write_bytes(b"".join(b"line %06d\n" % i for i in range(3000)))
    assert retention.trim_log(p, max_bytes=10_000, keep=1000)
    t = p.read_text()
    assert t.startswith("--- (older lines removed") and t.rstrip().endswith("line 002999") and len(t) < 1200
    assert not retention.trim_log(p, max_bytes=10_000, keep=1000)


def test_usage(tmp_path):
    d, h = setup(tmp_path)
    u = retention.usage(d)
    assert u["bytes"]["recordings"] == 4000 and u["oldest_day"] == "2026-11-17"


def test_the_operator_sets_how_long_and_six_weeks_is_the_default(tmp_path):
    from station.core import Station
    st = Station.__new__(Station)
    st.data, st.cfg, st.me = tmp_path, {}, "N0CALL"
    import threading
    st.lock = threading.RLock()
    assert st.keep_weeks() == 6
    st.cfg["keep_weeks"] = 8
    assert st.keep_weeks() == 8                                  # station.yaml
    st._save({"operator": {"keep_weeks": 3}})
    assert st.keep_weeks() == 3                                  # Settings wins
    st._save({"operator": {"keep_weeks": 500}})
    assert st.keep_weeks() == retention.MAX_WEEKS


def test_a_file_in_use_is_skipped_quietly(tmp_path, monkeypatch):
    rec = tmp_path / "recordings" / "20261101"
    rec.mkdir(parents=True)
    (rec / "a.wav").write_bytes(b"x")

    def refuse(p, *a, **k):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(retention.shutil, "rmtree", refuse)
    res = retention.sweep(tmp_path, 6, today=TODAY)
    assert res["in use"] == 1 and "errors" not in res and res["recordings"]["n"] == 0 and rec.exists()
