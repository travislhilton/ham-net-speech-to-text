"""Near-miss callsign correction: only one clearly better reading is ever applied; what was heard is kept."""
import copy
import time

import numpy as np
import pytest

from hamkit import fcc
from hamkit.callfix import CallFixer, Context, distance, edits
from station import engine
from station.core import Station
from tests.test_station import CFG, FakeBook, wait_for

# A tiny FCC database shaped like the real one (usi, call, status / city, state).
LICENCES = [("K7XVG", "A", "WAVERLY", "ID"), ("K7XDB", "A", "SPOKANE", "WA"),
            ("KA7XP", "A", "CYPRESS CREEK", "IL"), ("KA7XT", "E", "FAIRVIEW", "AZ"),
            ("KA7ST", "A", "OGDENVILLE", "IL"), ("KD7XYA", "A", "DUNMORE", "ID"),
            ("KB0XHR", "A", "PLAINVIEW", "CO"), ("KD0XHR", "E", "LAKEVIEW", "MN"),
            ("W9XYZ", "A", "NORTH HAVERBROOK", "IL"), ("W7XYM", "A", "OGDENVILLE", "IL"),
            ("WA7CXW", "A", "HARLAN", "OR"), ("WA7DXW", "A", "MESA", "AZ"),
            ("KE7ABC", "A", "SPRINGFIELD", "IL"), ("KG7ABC", "A", "CAPITAL CITY", "IL")]


@pytest.fixture
def con(tmp_path):
    c = fcc.connect(tmp_path / "fcc.sqlite")
    for usi, (call, st, city, state) in enumerate(LICENCES, 1):
        c.execute("INSERT INTO hd VALUES (?,?,?,?,?,?)", (usi, call, st, "2020-01-01", "2030-01-01", None))
        c.execute("INSERT INTO en (usi, call, city, state) VALUES (?,?,?,?)", (usi, call, city, state))
    c.commit()
    return c


def fixer(con):
    known = {c for c, *_ in LICENCES}
    return CallFixer(con, None, lambda c: c in known)


def ctx(**kw):
    kw.setdefault("me", "W9XYZ"); kw.setdefault("home_state", "IL"); kw.setdefault("districts", {"7"})
    return Context(**kw)


def test_distance_and_edits():
    assert distance("WB6BQW", "WB6DQW") == 0.5                 # B/D rhyme
    assert distance("WB08Q", "WD0AQ") == 1.0                     # two rhymes: B/D, 8/A
    assert distance("W7XYZ", "W9XYZ") == 1.0                   # a digit
    assert distance("KA7XT", "KA7XTA") == 1.0                  # an insertion
    assert distance("K7ABC", "N3XYZ") is None
    e = edits("K7XDG")
    assert e["K7XVG"] == 0.5 and e["K7DG"] == 1.0 and e["KK7XDG"] == 1.0 and "K7XDGA" not in e and "K7XDG" not in e
    assert all(len(c) <= 7 for c in e) and len(e) < 400


def test_city_named_in_the_transmission_picks_the_licence(con):
    text, calls, corr, maybe = fixer(con).fix("Have a terrific evening. K7XDG Waverly.", [], ctx())
    assert text == "Have a terrific evening. K7XVG Waverly." and calls == ["K7XVG"]
    assert corr == [{"heard": "K7XDG", "call": "K7XVG", "why": "FCC licence in Waverly, named in this transmission"}]


def test_fcc_alone_never_corrects(con):
    """Without context or a spoken city a near FCC licence is never applied (1.7 M licences: most garbles have
    several); one in the station's own state is offered as a 'maybe'."""
    text, calls, corr, maybe = fixer(con).fix("Go ahead K7XDG.", [], ctx())
    assert text == "Go ahead K7XDG." and calls == [] and corr == [] and maybe == []
    text, calls, corr, maybe = fixer(con).fix("Go ahead W7XYN.", [], ctx(me="K7ABC"))
    assert corr == [] and maybe == [{"heard": "W7XYN", "candidates": ["W7XYM"]}]


def test_a_call_heard_in_this_net_corrects_one_rhyme(con):
    text, calls, corr, _ = fixer(con).fix("Thanks WA7BXW.", [], ctx(net={"WA7CXW": 1}))
    assert calls == ["WA7CXW"] and corr[0]["why"] == "heard in this net" and text == "Thanks WA7CXW."


def test_lapsed_licence_corrected_by_the_net_and_the_city(con):
    """'KA7XT, Cypress Greek': KA7XT is an expired out-of-state licence; KA7XP of Cypress Creek checked in."""
    t = "Have a grand return on KA7XT, Cypress Greek."
    text, calls, corr, _ = fixer(con).fix(t, ["KA7XT"], ctx(net={"KA7XP": 1}))
    assert calls == ["KA7XP"] and "KA7XP" in text and corr[0]["heard"] == "KA7XT"
    # without the net it stays as heard (KA7ST is just as near)
    text, calls, corr, maybe = fixer(con).fix(t, ["KA7XT"], ctx())
    assert calls == ["KA7XT"] and corr == [] and maybe


def test_active_licences_are_never_touched(con):
    text, calls, corr, maybe = fixer(con).fix("KG7ABC and KE7ABC", ["KG7ABC", "KE7ABC"], ctx(net={"KE7ABC": 3}))
    assert calls == ["KG7ABC", "KE7ABC"] and corr == [] and maybe == []


def test_own_call_is_only_a_maybe(con):
    """Whisper's prompt carries the operator's call, so it also invents near copies of it: never auto-correct."""
    text, calls, corr, maybe = fixer(con).fix("W7XYZ, BLM.", [], ctx(net={"W9XYZ": 4}))
    assert corr == [] and maybe == [{"heard": "W7XYZ", "candidates": ["W9XYZ"]}]


def test_two_rhymes_need_two_sightings(con):
    f = CallFixer(None, None, lambda c: False)                 # no FCC: the net alone
    _, calls, corr, maybe = f.fix("Got WB08Q.", [], ctx(net={"WD0AQ": 1}))
    assert corr == [] and maybe[0]["candidates"] == ["WD0AQ"]
    text, calls, corr, _ = f.fix("Got WB08Q.", [], ctx(net={"WD0AQ": 2}))
    assert text == "Got WD0AQ." and corr[0] == {"heard": "WB08Q", "call": "WD0AQ", "why": "heard 2 times in this net"}


def test_two_net_calls_equally_near_stay_as_heard():
    f = CallFixer(None, None, lambda c: False)
    _, calls, corr, maybe = f.fix("WB6BQW here", [], ctx(net={"WB6DQW": 1, "WB6PQW": 1}))
    assert corr == [] and set(maybe[0]["candidates"]) == {"WB6DQW", "WB6PQW"}


def test_written_apart_and_hyphenated(con):
    text, calls, corr, _ = fixer(con).fix("Thank you. KD7, XYA Dunmore.", [], ctx())
    assert text == "Thank you. KD7XYA Dunmore." and calls == ["KD7XYA"] and corr[0]["heard"] == "KD7, XYA"
    text, calls, corr, _ = fixer(con).fix("KD-0XHR Plainview.", [], ctx())
    assert text == "KB0XHR Plainview." and corr[0]["heard"] == "KD-0XHR"
    text, calls, corr, _ = fixer(con).fix("KG7, ABC is mobile.", [], ctx())   # real, but nothing says it is him
    assert calls == [] and corr == []
    text, calls, corr, _ = fixer(con).fix("KG7-ABC and the ICS-213.", [], ctx())  # an active call: joined, not changed
    assert text == "KG7ABC and the ICS-213." and calls == ["KG7ABC"] and corr == []


def test_dx_call_corrected_only_toward_the_net(con):
    """'M7OKF' passes the country file (England) but N7OKF checked in; an unrelated DX call is left alone."""
    f = CallFixer(con, None, lambda c: True)
    _, calls, corr, _ = f.fix("M7OKF, Whiskey.", ["M7OKF"], ctx(net={"N7OKF": 1}))
    assert calls == ["N7OKF"] and corr[0]["heard"] == "M7OKF"
    _, calls, corr, _ = f.fix("G4ABC portable.", ["G4ABC"], ctx(net={"N7OKF": 1}))
    assert calls == ["G4ABC"] and corr == []


def test_lookup_is_fast(con):
    f = fixer(con)
    t = time.perf_counter()
    for _ in range(50):
        f.fix("K7XDG Waverly, KD7, XYA Dunmore, WA7BXW and KA7XT.", ["KA7XT"], ctx(net={"WA7CXW": 1}))
    assert (time.perf_counter() - t) / 50 < 0.05


# ---------- in the station ----------
def test_station_corrects_live_and_accurate_text(tmp_path):
    texts = ["This is WB6DQW checking in.", "Thanks WB6BQW, you're in."]
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(texts, "fake-final"),
                 callbook=FakeBook({"WB6DQW": "Dale", "W9XYZ": "Pat"}), data_dir=tmp_path)
    st.start(listen=False)
    try:
        st.inject("ftm", np.zeros(16000 * 3, np.float32))
        assert wait_for(lambda: st.tx and st.tx[-1]["status"] == "final")
        st.inject("ftm", np.zeros(16000 * 3, np.float32))
        assert wait_for(lambda: len(st.tx) == 2 and st.tx[-1]["status"] == "final")
        first, tx = st.tx
        assert tx["text"] == "Thanks WB6DQW, you're in." and tx["callsigns"] == ["WB6DQW"]
        assert tx["corrections"] == [{"heard": "WB6BQW", "call": "WB6DQW", "why": "heard in this net"}]
        assert st.heard["WB6DQW"]["count"] == 2 and "WB6BQW" not in st.heard
        # the accurate pass sees the whole net: a garble in the FIRST transmission is fixed by the second
        st.apply_accurate(first["id"], {"text": "This is WB6BQW checking in.", "model": "fake-large"})
        acc = st.tx[0]["accurate"]
        assert acc["callsigns"] == ["WB6DQW"] and acc["speaker"] == "WB6DQW" and acc["corrections"][0]["heard"] == "WB6BQW"
        p = st.winlink_preview("ftm")
        assert [x["call"] for x in p["to"] if x["call"] != "W9XYZ"] == ["WB6DQW", "K7NET"]   # + net control (speaker rules)
        again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), callbook=FakeBook({"WB6DQW": "Dale"}),
                        data_dir=tmp_path)
        again.load_today()
        assert again.tx[-1]["corrections"][0]["heard"] == "WB6BQW"                       # kept in the log
    finally:
        st.stop.set()


def test_station_can_turn_it_off(tmp_path):
    cfg = copy.deepcopy(CFG); cfg["callfix"] = False
    st = Station(cfg, lambda: engine.FakeEngine(["This is WB6DQW.", "Thanks WB6BQW."]),
                 callbook=FakeBook({"WB6DQW": "Dale"}), data_dir=tmp_path)
    st.start(listen=False)
    try:
        for n in (1, 2):
            st.inject("ftm", np.zeros(16000 * 3, np.float32))
            assert wait_for(lambda: len(st.tx) == n and st.tx[-1]["status"] == "final")
        assert st.tx[-1]["callsigns"] == [] and "corrections" not in st.tx[-1]
    finally:
        st.stop.set()


def test_ended_net_is_corrected_with_the_whole_net(tmp_path):
    from datetime import datetime, timedelta
    texts = ["Good evening, all.", "This is WB6DQW checking in."]
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(texts, "fake-final"),
                 callbook=FakeBook({"WB6DQW": "Dale", "W9XYZ": "Pat"}), data_dir=tmp_path)
    st.start(listen=False)
    try:
        st.inject("ftm", np.zeros(16000 * 3, np.float32))
        assert wait_for(lambda: st.tx and st.tx[-1]["status"] == "final")
        first = st.tx[0]
        st.apply_accurate(first["id"], {"text": "This is WB6BQW, good evening.", "model": "fake-large"})
        assert not st.tx[0]["accurate"].get("corrections")              # nothing to go on yet
        st.inject("ftm", np.zeros(16000 * 3, np.float32))
        assert wait_for(lambda: len(st.tx) == 2 and st.tx[-1]["status"] == "final")
        st.refix_ended_nets(datetime.now())                              # still going: left alone
        assert not st.tx[0]["accurate"].get("corrections")
        st.refix_ended_nets(datetime.now() + timedelta(minutes=16))      # over: the whole net is known
        acc = st.tx[0]["accurate"]
        assert acc["callsigns"] == ["WB6DQW"] and acc["corrections"][0]["heard"] == "WB6BQW"
        st.refix_ended_nets(datetime.now() + timedelta(minutes=17))      # once per net
        again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), callbook=FakeBook({"WB6DQW": "Dale"}), data_dir=tmp_path)
        again.load_today()
        assert again.tx[0]["accurate"]["callsigns"] == ["WB6DQW"]
    finally:
        st.stop.set()
