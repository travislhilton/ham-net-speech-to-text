"""Tap-to-correct: the operator's text wins everywhere, survives a restart, can be undone; the station learns
fix-ups from repeated corrections (only after Accept) and measures its accuracy against them."""
import copy
import json
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from station import corrections, engine, ics, speaker, wer
from station.core import Station
from station.server import serve
from tests.net_fixtures import ARES, lines
from tests.test_history import CFG, TUESDAYS, write_logs

DAY = TUESDAYS[-1]


def make(tmp_path):
    write_logs(tmp_path, lines(ARES, DAY, first_id=100))
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=lambda: DAY)
    st.load_today()
    st.history.catch_up(tmp_path / "logs")
    return st


def restart(tmp_path):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=lambda: DAY)
    st.load_today()
    return st


def log_lines(tmp_path):
    return [json.loads(x) for x in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()]


def add_line(tmp_path, tid, minute, text):
    write_logs(tmp_path, [{"id": tid, "radio": "ftm", "start": DAY.replace(minute=minute).isoformat(),
                           "status": "final", "text": text, "callsigns": [], "freq_mhz": 146.94}])


def test_wer_align_merges_a_run_of_differences():
    a, b = "this is k seven a b c checking in".split(), "this is k7abc checking in".split()
    assert wer.align(a, b) == [("equal", 0, 2, 0, 2), ("replace", 2, 7, 2, 3), ("equal", 7, 9, 3, 5)]
    assert wer.align([], ["x"]) == [("insert", 0, 0, 0, 1)]
    assert wer.align(["x"], []) == [("delete", 0, 1, 0, 0)]
    assert wer.align(["a"], ["a"]) == [("equal", 0, 1, 0, 1)]


def test_substitutions_keep_short_ones_and_callsigns():
    s = corrections.substitutions("K7ABC, Springfeld, no traffic.", "K7ABC, Springfield, no traffic.")
    assert s == [{"heard": "springfeld", "say": "Springfield", "call": False}]
    s = corrections.substitutions("This is kilo seven alpha bee sea checking in", "This is K7ABC checking in")
    assert s == [{"heard": "kilo seven alpha bee sea", "say": "K7ABC", "call": True}]
    assert corrections.substitutions("one two three four five six", "a b c d e f") == []       # a rewrite, not a fix
    assert corrections.substitutions("no traffic", "no traffic tonight") == []                  # an insertion alone
    assert corrections.substitutions("Roger.", "roger") == []                                   # case/punctuation only


def test_correct_wins_everywhere_survives_restart_and_undo(tmp_path):
    st = make(tmp_path)
    tid = "101"                                           # "K7ABC, Springfield, no traffic."
    q = st.bus.subscribe()
    out = st.correct(tid, "  KD9AAA,   Springfield, no traffic.  ")
    assert out["corrected"]["text"] == "KD9AAA, Springfield, no traffic." and out["corrected"]["by"] == "operator"
    assert out["corrected"]["callsigns"] == ["KD9AAA"]
    assert out["corrected"]["from"] == "K7ABC, Springfield, no traffic."
    assert speaker.best(out) == ("KD9AAA", "stated")       # callsigns and the speaker come from the corrected text
    kinds = []
    while not q.empty():
        kinds.append(q.get()[0])
    assert "tx" in kinds and "inst" in kinds               # other dashboards refresh: the log and the minutes
    up = [x for x in log_lines(tmp_path) if x.get("update") == tid]
    assert up[-1]["corrected"]["text"] == "KD9AAA, Springfield, no traffic."
    # precedence: corrected > accurate > live, also after a later accurate pass
    st.apply_accurate(tid, {"text": "K7ABC Springfield no traffic", "model": "large-v3"})
    v = ics.view(st._find(tid))
    assert v["text"] == "KD9AAA, Springfield, no traffic." and v["callsigns"] == ["KD9AAA"]
    assert "KD9AAA" in st.heard
    # history: search, minutes transcript, attendance
    assert [t["id"] for t in st.history.search("KD9AAA Springfield")["tx"]] == [tid]
    inst = st.history.instance("ares@2026-11-03")
    row = next(t for t in inst["tx"] if t["id"] == tid)
    assert row["text"] == "KD9AAA, Springfield, no traffic." and row["corrected"]["by"] == "operator"
    assert row["live_text"] == "K7ABC, Springfield, no traffic."
    assert "KD9AAA" in {p["call"] for p in inst["participants"] if p["role"] != "mentioned"}
    # ICS-309 / Winlink use it
    span = ics.select(st.tx, "ftm", DAY.isoformat(), DAY.replace(hour=23).isoformat())
    assert next(t for t in span if t["id"] == tid)["text"].startswith("KD9AAA")
    # replayed by load_today on a restart (after the accurate pass line too)
    t = restart(tmp_path)._find(tid)
    assert t["corrected"]["text"].startswith("KD9AAA") and t["accurate"]["model"] == "large-v3"
    # undo restores the accurate text (and is replayed too)
    out = st.correct(tid, undo=True)
    assert "corrected" not in out and ics.view(st._find(tid))["text"] == "K7ABC Springfield no traffic"
    assert next(t for t in st.history.instance("ares@2026-11-03")["tx"] if t["id"] == tid)["corrected"] is None
    assert "corrected" not in restart(tmp_path)._find(tid)
    with pytest.raises(ValueError):
        st.correct(tid, "   ")
    with pytest.raises(LookupError):
        st.correct("nope", "x")


def test_history_rebuilt_from_logs_keeps_corrections(tmp_path):
    st = make(tmp_path)
    st.correct("104", "KD9AAA, mobile, with one item for the net. Over.")
    st.history.close()
    for f in tmp_path.glob("history.db*"):
        f.unlink()
    again = restart(tmp_path)
    again.history.catch_up(tmp_path / "logs")
    rows = again.history.corrected_rows()
    assert [r["id"] for r in rows] == ["104"] and rows[0]["corrected"]["text"].endswith("Over.")


def test_a_correction_to_an_older_day_goes_through_the_history(tmp_path):
    write_logs(tmp_path, lines(ARES, TUESDAYS[0], first_id=500))
    st = make(tmp_path)
    assert st._find("501") is None                        # not today: not in memory
    out = st.correct("501", "K7ABC, Springfield, no traffic tonight.")
    assert out["corrected"]["text"].endswith("tonight.")
    assert any(t["id"] == "501" for t in st.history.search("tonight")["tx"])


def test_learning_proposes_only_repeats_and_waits_for_accept(tmp_path):
    st = make(tmp_path)
    st.correct("101", "K7ABC, Springfield, no traffic.")          # unchanged: nothing to learn
    st.correct("103", "W9XYZ, Shelbyville, no traffic, short time.")
    v = st.learned()
    assert v["proposals"] == [] and v["corrections"] == 2
    add_line(tmp_path, "200", 40, "N0CALL, checking in from Shelby Ville, no traffic.")
    add_line(tmp_path, "201", 41, "K7ABC, out to Shelby Ville tonight.")
    st.history.catch_up(tmp_path / "logs")
    st.correct("200", "N0CALL, checking in from Shelbyville, no traffic.")
    v = st.learned()
    assert v["proposals"] == [] and [p["heard"] for p in v["watching"]] == ["shelby ville"]   # once: watching
    st.correct("201", "K7ABC, out to Shelbyville tonight.")
    v = st.learned()
    assert [(p["heard"], p["say"], p["count"]) for p in v["proposals"]] == [("shelby ville", "Shelbyville", 2)]
    assert not (tmp_path / "fixups_learned.yaml").exists()           # never applied before Accept
    # a callsign fix is proposed at once
    st.correct("108", "K7NET, roger. KC7ABC will send the volunteer roster to everyone by Friday.")
    v = st.learned()
    assert ("k7abc", "KC7ABC") in {(p["heard"], p["say"]) for p in v["proposals"]}
    v = st.learned("accept", "Shelby Ville", "Shelbyville")
    assert v["accepted"][0]["heard"] == "shelby ville" and v["accepted"][0]["count"] == 2
    doc = yaml.safe_load((tmp_path / "fixups_learned.yaml").read_text())
    assert doc["fixups"] == [{"heard": "shelby ville", "say": "Shelbyville", "count": 2,
                              "first_at": v["accepted"][0]["first_at"]}]
    assert ("shelby ville", "Shelbyville") not in {(p["heard"], p["say"]) for p in v["proposals"]}
    v = st.learned("ignore", "k7abc", "KC7ABC")
    assert v["proposals"] == [] and v["ignored"] == 1
    v = st.learned("remove", "shelby ville", "Shelbyville")
    assert v["accepted"] == [] and yaml.safe_load((tmp_path / "fixups_learned.yaml").read_text())["fixups"] == []
    with pytest.raises(ValueError):
        st.learned("explode", "a", "b")


def test_accuracy_from_corrections(tmp_path):
    st = make(tmp_path)
    assert st.accuracy()["n"] == 0 and "No corrected lines" in st.accuracy()["note"]
    st.apply_accurate("101", {"text": "K7ABC, Springfield, no traffic.", "model": "large-v3"})
    st.correct("101", "K7ABC, Springfield, no traffic tonight.")       # live + accurate both miss one word
    st.correct("103", "W9XYZ, Shelbyville, no traffic.")               # live right
    a = st.accuracy()
    assert a["n"] == 2 and a["few"] and "rough guide" in a["note"]
    allg = next(g for g in a["groups"] if g["by"] == "all")
    assert allg["live"]["errors"] == 1 and allg["live"]["n"] == 2
    assert allg["accurate"]["n"] == 1 and allg["accurate"]["errors"] == 1
    assert {(g["by"], g["name"]) for g in a["groups"]} >= {("radio", "FTM-150R"), ("net", "Springfield ARES Net"), ("mode", "FM")}
    assert "consensus" not in allg
    txt = corrections.table(a)
    assert "2 corrected lines" in txt and "FTM-150R" in txt
    from tools import accuracy as cli
    assert cli.main(["--data", str(tmp_path), "--json"]) == 0


def test_consensus_text_is_measured_when_present():
    raw = {"id": "1", "radio": "ftm", "text": "a b c", "consensus": {"text": "a b d"},
           "corrected": {"text": "a b d", "at": "2026-11-03T19:00:00"}}
    g = corrections.accuracy([raw])["groups"][0]
    assert g["live"]["wer"] == pytest.approx(1 / 3, abs=1e-3) and g["consensus"]["wer"] == 0


def _post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def test_correct_learned_and_accuracy_endpoints(tmp_path):
    st = make(tmp_path)
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        d = _post(base, "/api/tx/101/correct", {"text": "KD9AAA, Springfield, no traffic."})
        assert d["corrected"]["text"].startswith("KD9AAA") and d["who"]["call"] == "KD9AAA"
        d = _post(base, "/api/tx/101/correct", {"undo": True})
        assert "corrected" not in d
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(base, "/api/tx/999999/correct", {"text": "x"})
        assert e.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(base, "/api/tx/101/correct", {"text": ""})
        assert e.value.code == 400
        _post(base, "/api/tx/103/correct", {"text": "W9XYZ, Shelbyville, no traffic, out."})
        acc = json.loads(urllib.request.urlopen(base + "/api/accuracy").read())
        assert acc["n"] == 1
        lv = json.loads(urllib.request.urlopen(base + "/api/learned").read())
        assert lv["corrections"] == 1 and "rule" in lv
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(base, "/api/learned", {"action": "accept", "heard": "", "say": "x"})
        assert e.value.code == 400
        lv = _post(base, "/api/learned", {"action": "accept", "heard": "springfeld", "say": "Springfield"})
        assert lv["accepted"][0]["say"] == "Springfield"
    finally:
        srv.shutdown()
