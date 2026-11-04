"""Net-wide callsign agreement (station/consensus.py): one station, one call, across a net; never invented.

The net below is a sanitized copy of the patterns in a real 140-transmission FM net (2026-11-03): every call,
name and town is made up. Variant readings of one station, net control thanking a station by first name in the
next transmission, a town said after the call, a clipped call ("87QF"), a call the transcript did not list, an
ambiguous "Bob" that fits two licences, two real stations one letter apart, and a call nothing supports."""
import copy
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

import pytest

from hamkit import fcc
from station import consensus as K, engine, ics
from station.core import Station
from tests.net_fixtures import lines
from tests.test_station import FakeBook

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 3, 20, 0)

# call, status, first name, city, state
LICENCES = [
    ("K7NET", "A", "Morgan", "SPRINGFIELD", "IL"),
    ("KF7AQT", "A", "Alice", "SPRINGFIELD", "IL"), ("KF7AGP", "E", "Daniel", "SHELBYVILLE", "ID"),
    ("N7YTX", "A", "WILLIAM", "OGDENVILLE", "IL"), ("N7YPX", "A", "LARRY", "CAPITAL CITY", "WA"),
    ("KE7ABC", "A", "Pat", "SPRINGFIELD", "IL"), ("KG7ABC", "A", "Chris", "SHELBYVILLE", "IL"),
    ("AD7QF", "A", "Brent", "SPRINGFIELD", "IL"), ("AE7QF", "A", "Dana", "NORTH HAVERBROOK", "OR"),
    ("W7KGQ", "A", "Kevin", "SPRINGFIELD", "IL"), ("W7KDQ", "A", "Kevin", "PORTLAND", "OR"),
    ("KD7PHX", "E", "Esther", "CAPITAL CITY", "WA"),
    ("KD7BHX", "A", "ROBERT", "SPRINGFIELD", "IL"), ("KD7DHX", "A", "Robert", "SHELBYVILLE", "IL"),
    ("K7RRD", "A", "Sam", "SPRINGFIELD", "IL"),
    ("KE7QZZ", "A", "Jo", "SPRINGFIELD", "IL"), ("KC7QVJ", "A", "Petra", "SPRINGFIELD", "IL"),
    ("KK7QPZ", "A", "Quincy", "SPRINGFIELD", "IL"), ("KM7QEZ", "A", "Quincy", "SHELBYVILLE", "IL"),
    ("KB7QXV", "A", "Quincy", "SPRINGFIELD", "IL"),
    ("KF7QLB", "A", "Scott", "SHELBYVILLE", "MT"), ("KF7QLV", "A", "Dave", "SPRINGFIELD", "IL"),
    ("N7QH", "A", "Lee", "SPRINGFIELD", "IL"), ("AB7QH", "A", "Lee", "OGDENVILLE", "IL"),
]

SCRIPT = [  # minute, accurate text, callsigns as normalize() accepted them
    (0, "This is K7NET, net control for the Springfield County Aries net.", ["K7NET"]),
    (1, "KF7AQT.", ["KF7AQT"]),
    (1.2, "KF7AGP in Springfield on handheld. Thank you, Alice. N7YPX.", ["KF7AGP", "N7YPX"]),
    (1.4, "N7YPX.", ["N7YPX"]),
    (1.6, "Thank you Bill, KE7ABC.", ["KE7ABC"]),
    (1.8, "KE7ABC, Pat in Springfield.", ["KE7ABC"]),
    (2.0, "Thank you, Pat. KG7ABC.", ["KG7ABC"]),
    (2.2, "KG7ABC, Chris in Shelbyville, commercial power.", ["KG7ABC"]),
    (2.4, "Thank you, Chris. 87QF.", []),
    (2.6, "87QF, Brent, with handheld.", []),
    (2.8, "You're good, Brent. Thank you.", []),
    (3.0, "This is W7KTQ commercial power.", []),
    (3.2, "Thank you, Kevin. KD7PHX.", ["KD7PHX"]),
    (3.4, "KD7PHX, running on battery.", ["KD7PHX"]),
    (3.6, "Thank you, Bob. KZ7QQQ.", ["KZ7QQQ"]),
    (3.8, "K7RRT.", ["K7RRT"]),
    (4.0, "That ends the roll call. Send your Windlink message tonight.", []),
]


def make_fcc(path):
    c = fcc.connect(path)
    for usi, (call, st, first, city, state) in enumerate(LICENCES, 1):
        c.execute("INSERT INTO hd VALUES (?,?,?,?,?,?)", (usi, call, st, "2020-01-01", "2030-01-01", None))
        c.execute("INSERT INTO en (usi, call, first, city, state) VALUES (?,?,?,?,?)", (usi, call, first, city, state))
    c.commit()
    return c


@pytest.fixture
def lic(tmp_path):
    return K.Licences(make_fcc(tmp_path / "fcc.sqlite"))


def net_lines(first_id=7000):
    return lines([(m, t, c, []) for m, t, c in SCRIPT], T0, first_id=first_id, ncs="K7NET")


def views():
    return [ics.view(t) for t in net_lines()]


def run(lic, **kw):
    kw.setdefault("home_state", "IL")
    return K.agree(views(), lic, **kw)


def test_distance_knows_the_letters_whisper_confuses():
    assert K.distance("N7YPX", "N7YTX") == 0.5               # P/T rhyme
    assert K.distance("K17ABC", "K77ABC") == 0.5             # 1/7
    assert K.distance("KK7QD", "KK7QCN") == 1.5              # D/C and a dropped N
    assert K.distance("K7ABC", "W9XYZ") is None
    n = K.neighbours("K7RRT")
    assert n["K7RRD"] == 0.5 and all(K.US_CALL.match(c) for c in n)


def test_first_names_and_nicknames():
    assert {"ROBERT", "BOB"} <= K.first_names("Robert") and "WALLY" in K.first_names("WALLACE")
    assert "ROBERT" in K.first_names("Bob") and K.first_names(None) == set() and K.first_names("J") == set()


def test_variants_of_one_station_agree_by_name_and_town(lic):
    r = run(lic)
    e = r["calls"]["KF7AGP"]
    assert e["call"] == "KF7AQT" and e["status"] == "fixed"
    assert "name Alice matches" in e["why"] and "town Springfield said" in e["why"]
    assert r["calls"]["KF7AQT"]["status"] == "verified"
    # never heard as N7YTX: an Active licence in the state whose holder net control thanked by his nickname
    e = r["calls"]["N7YPX"]
    assert e["call"] == "N7YTX" and "name Bill matches" in e["why"] and e["conf"] >= 0.9
    tx = r["tx"]
    assert tx["7002"] == ["KF7AQT", "N7YTX"] and tx["7003"] == ["N7YTX"]


def test_a_clipped_call_and_an_unlisted_one_are_found_only_with_a_name(lic):
    r = run(lic)
    assert r["calls"]["7QF"]["call"] == "AD7QF" and "name Brent matches" in r["calls"]["7QF"]["why"]
    assert r["tx"]["7008"] == ["AD7QF"] and r["tx"]["7009"] == ["AD7QF"]
    assert r["calls"]["W7KTQ"]["call"] == "W7KGQ"                    # Kevin in the state, not Kevin elsewhere
    assert r["tx"]["7011"] == ["W7KGQ"]
    # no name after it: a clipped call stays out of the calls
    v = [{"id": "1", "text": "Next is 87QF.", "callsigns": [], "freq_mhz": 146.94}]
    assert K.agree(v, lic, home_state="IL")["tx"]["1"] == []



def test_a_clipped_call_is_completed_only_when_one_licence_fits_and_its_name_or_history_backs_it(lic):
    ag = lambda s, **kw: K.agree(net(s), lic, home_state="IL", **kw)
    # net control's direct reply names the only in-state licence ending in 7QF (AE7QF is out of state)
    r = ag([("87QF.", []), (".", []), ("You're good, Brent.", [])])
    assert r["tx"]["0"] == ["AD7QF"] and "the only licence in IL ending in 7QF" in r["calls"]["7QF"]["why"]
    # the name after ANOTHER call is not the clipped call's; no name at all: left out
    assert ag([("87QF.", []), ("KE7ABC, Brent.", ["KE7ABC"])])["tx"]["0"] == []
    assert ag([("87QF.", []), ("Good evening.", [])])["tx"]["0"] == []
    # earlier check-ins to this net instead of a name
    assert ag([("He's 7QF.", []), ("Good evening.", [])], prior={"AD7QF": 3})["tx"]["0"] == ["AD7QF"]
    # two in-state licences end in 7QH: left as heard, with both as candidates
    r = ag([("7QH.", []), ("Thank you, Lee.", [])])
    assert r["tx"]["0"] == [] and r["calls"]["7QH"]["candidates"] == ["AB7QH", "N7QH"]


def net(script):
    return [{"id": str(i), "text": t, "callsigns": c, "freq_mhz": 146.94} for i, (t, c) in enumerate(script)]


def test_never_merge_calls_said_in_the_same_transmission_or_in_their_own_turns(lic):
    # two sound-alike letters (B/Z, G/Z) apart, the other one Active in the state: still two stations
    r = K.agree(net([("KE7QBG and KE7QZZ.", ["KE7QBG", "KE7QZZ"])]), lic, home_state="IL")
    assert r["calls"]["KE7QBG"]["call"] == "KE7QBG" and r["tx"]["0"] == ["KE7QBG", "KE7QZZ"]
    turns = [("KE7QBG.", ["KE7QBG"]), ("Good evening.", []), ("Next.", []), ("KE7QZZ.", ["KE7QZZ"])]
    assert K.agree(net(turns), lic, home_state="IL")["calls"]["KE7QBG"]["call"] == "KE7QBG"
    # one letter, and the other reading is no licence at all: one station read twice
    turns = [("KE7QZA.", ["KE7QZA"]), ("Good evening.", []), ("Next.", []), ("KE7QZZ.", ["KE7QZZ"])]
    assert K.agree(net(turns), lic, home_state="IL")["calls"]["KE7QZA"]["call"] == "KE7QZZ"


def test_a_live_licence_heard_is_never_moved_on_the_state_alone(lic):
    v = net([("KF7QLB and KE7ABC.", ["KF7QLB", "KE7ABC"])])
    live = {"0": ["KF7QLV", "KE7ABC"]}                    # the live text read the same words as the in-state call
    assert K.agree(v, lic, home_state="IL", live_calls=live)["calls"]["KF7QLB"]["call"] == "KF7QLB"
    v = net([("KF7QLB, Dave, handheld.", ["KF7QLB"])])
    assert K.agree(v, lic, home_state="IL", live_calls={"0": ["KF7QLV"]})["calls"]["KF7QLB"]["call"] == "KF7QLV"


def test_two_letters_need_the_name_with_the_call_or_name_and_history(lic):
    s = [("KT7QDJ.", ["KT7QDJ"]), ("Welcome Petra, great signal.", [])]
    assert K.agree(net(s), lic, home_state="IL")["calls"]["KT7QDJ"]["call"] == "KT7QDJ"    # name in the reply
    e = K.agree(net(s), lic, home_state="IL", prior={"KC7QVJ": 2})["calls"]["KT7QDJ"]
    assert e["call"] == "KC7QVJ" and "2 prior check-ins" in e["why"]
    s = [("KT7QDJ, Petra in Springfield.", ["KT7QDJ"])]
    assert K.agree(net(s), lic, home_state="IL")["calls"]["KT7QDJ"]["call"] == "KC7QVJ"    # said with the call


def test_a_name_belongs_to_the_call_it_follows_and_moves_one_call_only(lic):
    # "Quincy" after KK7QPD is net control's reply; then KM7QEV says "Quincy" with its own call
    s = [("KK7QPD.", ["KK7QPD"]), ("Welcome Quincy.", []), ("KM7QEV, Quincy here.", ["KM7QEV"]),
         ("Thank you.", [])]
    r = K.agree(net(s), lic, home_state="IL")
    assert r["calls"]["KM7QEV"]["call"] == "KM7QEZ"
    assert r["calls"]["KK7QPD"]["call"] == "KK7QPD"                  # the name already moved another call
    # a name after ANOTHER call is not this call's: "KB7QXZ" never gets the name said after KE7ABC
    s = [("KB7QXZ.", ["KB7QXZ"]), ("KE7ABC, Quincy.", ["KE7ABC"])]
    assert K.agree(net(s), lic, home_state="IL")["calls"]["KB7QXZ"]["call"] == "KB7QXZ"
    # the reply is the next transmission with words: an empty one or the call said again is skipped
    s = [("KK7QPD.", ["KK7QPD"]), (".", []), ("KK7QPD", ["KK7QPD"]), ("Welcome Quincy.", [])]
    assert K.agree(net(s), lic, home_state="IL")["calls"]["KK7QPD"]["call"] == "KK7QPZ"


def test_two_real_stations_a_letter_apart_are_not_merged(lic):
    r = run(lic)
    assert r["calls"]["KE7ABC"]["call"] == "KE7ABC" and r["calls"]["KG7ABC"]["call"] == "KG7ABC"
    assert r["tx"]["7006"] == ["KG7ABC"] and r["tx"]["7005"] == ["KE7ABC"]


def test_an_ambiguous_name_is_left_as_heard_with_its_candidates(lic):
    e = run(lic)["calls"]["KD7PHX"]
    assert e["call"] == "KD7PHX" and e["status"] == "lapsed"
    assert set(e["candidates"]) == {"KD7BHX", "KD7DHX"}           # two Roberts: no clear winner


def test_never_invent_a_call_without_evidence_but_history_is_evidence(lic):
    r = run(lic)
    assert r["calls"]["KZ7QQQ"] == {"call": "KZ7QQQ", "status": "unverified", "conf": r["calls"]["KZ7QQQ"]["conf"],
                                    "why": "not in the FCC database", "score": 0.0}
    assert r["calls"]["K7RRT"]["call"] == "K7RRT"                  # K7RRD exists and is near, nothing else
    r = run(lic, prior={"K7RRD": 4})
    e = r["calls"]["K7RRT"]
    assert e["call"] == "K7RRD" and "4 prior check-ins to this net" in e["why"]


def test_without_the_fcc_only_what_was_heard_counts():
    r = K.agree(views(), None, home_state="IL")
    assert all(e["call"] == h for h, e in r["calls"].items())
    assert r["calls"]["KF7AQT"]["status"] == "unverified"


def test_live_readings_count_and_records_are_idempotent(lic):
    rows = net_lines()
    rows[1]["accurate"] = {"text": rows[1]["text"], "callsigns": ["KF7AQT"]}
    rows[1]["callsigns"] = ["KF7AGP"]                                # the live text read it the other way
    fx = {"rules": [{"heard": "Windlink", "say": "Winlink", "source": "vocab"},
                    {"heard": "Aries", "say": "ARES", "context": r"\bnet\b", "source": "vocab"}], "net_names": []}
    recs, res = K.for_session(rows, lic, {}, "IL", fix_cfg=fx)
    again, res2 = K.for_session(copy.deepcopy(rows), lic, {}, "IL", fix_cfg=fx)
    assert recs == again and res == res2
    assert recs["7002"]["callsigns"] == ["KF7AQT", "N7YTX"]
    assert {m["heard"]: m["call"] for m in recs["7002"]["moved"]} == {"KF7AGP": "KF7AQT", "N7YPX": "N7YTX"}
    assert recs["7016"]["text"] == "That ends the roll call. Send your Winlink message tonight."
    assert recs["7000"]["text"].endswith("Springfield County ARES net.")
    assert "7001" not in recs                                        # nothing to say: no record
    assert recs["7014"]["unverified"] == ["KZ7QQQ"]


def test_the_view_applies_a_fresh_record_and_only_the_mapping_of_a_stale_one(lic):
    rows = net_lines()
    recs, _ = K.for_session(rows, lic, {}, "IL", fix_cfg={"rules": [{"heard": "windlink", "say": "Winlink",
                                                                      "source": "learned"}]})
    t = dict(rows[2], consensus=recs["7002"])
    v = ics.view(t)
    assert v["callsigns"] == ["KF7AQT", "N7YTX"] and ics.view(t, agreed=False)["callsigns"] == ["KF7AGP", "N7YPX"]
    stale = dict(t, accurate={"text": "N7YPX and KF7AGP again.", "callsigns": ["N7YPX", "KF7AGP"]})
    assert ics.view(stale)["callsigns"] == ["N7YTX", "KF7AQT"]
    fixed = dict(rows[16], consensus=recs["7016"])
    fv = ics.view(fixed)
    assert fv["text"].endswith("Winlink message tonight.") and fv["heard_text"].endswith("Windlink message tonight.")
    assert K.calls_of(t) == ["KF7AQT", "N7YTX"]
    corrected = dict(t, corrected={"text": "KF7AGP, N7YPX.", "callsigns": ["KF7AGP", "N7YPX"]})
    assert ics.view(corrected)["callsigns"] == ["KF7AGP", "N7YPX"]   # the operator's correction always wins


def test_report_counts():
    v = [{"id": "1", "callsigns": ["A", "B"]}, {"id": "2", "callsigns": ["A"]}]
    assert K.report(v) == {"distinct": 2, "once": 1, "mentions": 3}
    assert K.report(v, {"tx": {"1": ["A"], "2": ["A"]}}) == {"distinct": 1, "once": 0, "mentions": 2}


# ---------- in the station: the log, stations heard, history, the forms, a restart ----------
class Book(FakeBook):
    def __init__(self, con):
        super().__init__({c: f for c, _, f, *_ in LICENCES})
        self.con, self.lock = con, threading.Lock()


CFG = {"callsign": "W9XYZ", "region_state": "IL",
       "radios": [{"id": "ftm", "name": "FTM-150R", "device": "x", "gate": "carrier", "freq_mhz": 146.94}],
       "schedule": [], "port": 0, "bind": "127.0.0.1", "qrz_online": False}


def station(tmp_path, con):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), callbook=Book(con), data_dir=tmp_path,
                 clock=lambda: datetime(2026, 11, 3, 21, 0))
    return st


def test_station_agreement_reaches_heard_history_forms_and_survives_a_restart(tmp_path):
    con = make_fcc(tmp_path / "fcc.sqlite")
    con.close()
    import sqlite3
    con = sqlite3.connect(tmp_path / "fcc.sqlite", check_same_thread=False)
    st = station(tmp_path / "data", con)
    for e in net_lines():
        st.tx.append(e); st._log(e)
    st._rebuild_heard()
    assert "KF7AGP" in st.heard and "AD7QF" not in st.heard
    start, end = st.tx[0]["start"], st.tx[-1]["start"]
    out = st.agree_session("ftm", start, end)
    assert out["changed"] >= 6 and out["moved"]["KF7AGP"]["call"] == "KF7AQT"
    assert "KF7AGP" not in st.heard and "N7YPX" not in st.heard
    assert {"KF7AQT", "N7YTX", "AD7QF", "W7KGQ", "KZ7QQQ"} <= set(st.heard)
    assert st.heard["AD7QF"]["card"]                                 # a found call gets its card
    assert st.agree_session("ftm", start, end)["changed"] == 0       # idempotent
    assert st.agree_recent()[0]["changed"] == 0
    log = (tmp_path / "data" / "logs" / "20261103.jsonl").read_text().splitlines()
    ups = [json.loads(x) for x in log if '"consensus"' in x]
    assert len(ups) == out["changed"] and all(u["consensus"]["src"] for u in ups)
    # the forms and the history see the agreed calls; the heard text is kept
    span = ics.select(st.tx, "ftm", start, end)
    people = [p["call"] for p in ics.participants(span, {"W9XYZ"}, st.heard)]
    assert "N7YTX" in people and "N7YPX" not in people and "AD7QF" in people
    st.history.flush()
    key = st.history.instance_of("ftm", start)
    parts = {p["call"] for p in st.history.instance(key)["participants"]}
    assert "KF7AQT" in parts and "KF7AGP" not in parts
    tx16 = next(t for t in st.history.instance(key)["tx"] if t["id"] == "7016")
    assert "Winlink" in tx16["text"] and "Windlink" in tx16["live_text"]
    # a restart reads the agreement back from the day log
    again = station(tmp_path / "data", con)
    again.load_today()
    assert "N7YTX" in again.heard and "N7YPX" not in again.heard
    # the operator corrects a line: the correction wins, the agreement's earlier record stays (for accuracy)
    t2 = st._find("7002")
    kept = dict(t2["consensus"])
    t2["corrected"] = {"text": "KF7AGP, N7YPX.", "callsigns": ["KF7AGP", "N7YPX"]}
    st.agree_session("ftm", start, end)
    assert st._find("7002")["consensus"] == kept and ics.view(st._find("7002"))["callsigns"] == ["KF7AGP", "N7YPX"]
    # new accurate text: the agreement made from the old text is dropped (and made again next time)
    st.apply_accurate("7003", {"text": "N7YPX.", "model": "m", "stage": "final"})
    assert "consensus" not in st._find("7003")


def test_history_prior_checkins(tmp_path):
    from tests.test_history import TUESDAYS, five_weeks, hist
    h = hist(tmp_path)
    for e in five_weeks():
        h.ingest(e)
    h.flush()
    p = h.prior_checkins("ares", TUESDAYS[-1].isoformat())
    assert p["K7ABC"] == 4 and p["W9XYZ"] == 3 and "AE7QQ" not in p
    assert h.prior_checkins(None, "2030") == {}


def test_cli_reruns_a_day_and_is_idempotent(tmp_path, capsys):
    from tools import consensus as cli
    data, cfgdir = tmp_path / "data", tmp_path / "config"
    (data / "logs").mkdir(parents=True); cfgdir.mkdir()
    shutil.copy(ROOT / "config" / "vocab.yaml", cfgdir / "vocab.yaml")
    make_fcc(tmp_path / "fcc.sqlite").close()
    log = data / "logs" / "20261103.jsonl"
    log.write_text("".join(json.dumps(e) + "\n" for e in net_lines()))
    args = ["2026-11-03", "--data", str(data), "--config", str(cfgdir), "--fcc", str(tmp_path / "fcc.sqlite"),
            "--state", "IL", "--net-name", "Springfield County ARES net"]
    assert cli.main(args + ["--dry-run"]) == 0
    assert "nothing written" in capsys.readouterr().out and '"consensus"' not in log.read_text()
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "KF7AGP -> KF7AQT" in out and "written" in out
    n = log.read_text().count('"consensus"')
    assert n >= 6
    assert cli.main(args) == 0
    assert "0 transmission records" in capsys.readouterr().out and log.read_text().count('"consensus"') == n
    merged = {t["id"]: t for t in cli.merged_log(log)}
    assert ics.view(merged["7002"])["callsigns"] == ["KF7AQT", "N7YTX"]
