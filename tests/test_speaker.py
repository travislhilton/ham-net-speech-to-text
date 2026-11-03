"""Who is talking: text rules (lines from the 3.915 MHz net of 2026-11-02 and typical VHF traffic), the operator's
"Who?" (persisted, wins, survives a restart and the accurate pass), the ICS-309 From, voice backfill logic and the
voice-print features."""
import copy
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from station import engine, ics, speaker
from station.core import Station
from station.server import serve, with_who
from tests.test_station import CFG, FakeBook, wait_for


@pytest.mark.parametrize("text,calls,who", [
    # self-identification
    ("Net control, this is K7ABC checking in. Over.", ["K7ABC"], "K7ABC"),
    ("K7ABC, this is W9XYZ, go ahead.", ["K7ABC", "W9XYZ"], "W9XYZ"),
    ("K7ABC, W9XYZ, go ahead", ["K7ABC", "W9XYZ"], "W9XYZ"),             # THEIR call, MY call, go ahead
    ("W9XYZ de K7ABC", ["W9XYZ", "K7ABC"], "K7ABC"),
    ("K7XVS, here it is.", ["K7XVS"], "K7XVS"),
    ("Nothing heard on the frequency, W9XYZ clear.", ["W9XYZ"], "W9XYZ"),
    ("Good evening all, signals are fine here, K7ABC over.", ["K7ABC"], "K7ABC"),
    ("This is NR7QQ, no traffic.", ["NR7QQ"], "NR7QQ"),
    # bare check-ins (call + place, little else)
    ("KE7XHT Ogdenville.", ["KE7XHT"], "KE7XHT"),
    ("W7ETQ Capital City. W7ETQ.", ["W7ETQ"], "W7ETQ"),
    ("AC6SW, Brockway.", ["AC6SW"], "AC6SW"),
    # net control reading back / addressing: the call is the ADDRESSEE, not the speaker
    ("K7ABC, thank you, you're in the log.", ["K7ABC"], None),
    ("Roger K7ABC, good evening.", ["K7ABC"], None),
    ("Let's try AT7XV.", ["AT7XV"], None),
    ("We had a mobile check-in from W7XSE, now calling and", ["W7XSE"], None),
    ("That brings us to number 60, KB7XT, Samus.", ["KB7XT"], None),
    ("W7TDQ, welcome aboard.", ["W7TDQ"], None),
    ("Got WD0AQ.", ["WD0AQ"], None),
    # an acknowledgement that ENDS in a call + place is ambiguous on a fast net (net control's read-back, or the
    # next station checking in, cut into one transmission): no guess
    ("In without traffic. Thank you, Garland, for your check-in tonight. N7OKF, Elmwood.", ["N7OKF"], None),
    ("K7ABC, W7XYZ and N0QZZ checked in so far, any others?", ["K7ABC", "W7XYZ", "N0QZZ"], None),
    ("no calls at all", [], None),
])
def test_stated_speaker(text, calls, who):
    assert speaker.stated(text, calls) == who


def test_net_control_acknowledges():
    assert speaker.from_text("K7ABC, thank you, you're in the log.", ["K7ABC"], ncs="w9xyz") == ("W9XYZ", "ncs")
    assert speaker.from_text("Got you in without any traffic there, Kevin. Have a terrific evening.", [], "K7NET") == ("K7NET", "ncs")
    assert speaker.from_text("This is K7ABC, no traffic.", ["K7ABC"], "K7NET") == ("K7ABC", "stated")
    assert speaker.from_text("Weather is clear here.", [], "K7NET") == (None, None)       # not net control's words
    assert speaker.from_text("K7ABC, thank you.", ["K7ABC"]) == (None, None)            # no schedule = no net control


def test_best_speaker_order():
    tx = {"text": "K7ABC, thank you.", "callsigns": ["K7ABC"], "speaker": "K7NET", "speaker_how": "ncs",
          "speaker_voice": {"call": "N0QZZ", "sim": 0.7}}
    assert speaker.best(tx) == ("K7NET", "ncs")
    tx["accurate"] = {"text": "This is W7XYZ.", "callsigns": ["W7XYZ"], "speaker": "W7XYZ", "speaker_how": "stated"}
    assert speaker.best(tx) == ("W7XYZ", "stated")
    tx["speaker_set"] = "KA7SU"
    assert speaker.best(tx) == ("KA7SU", "operator")
    assert speaker.best({"speaker_voice": {"call": "N0QZZ", "sim": 0.7}}) == ("N0QZZ", "voice")
    # logged before speaker_how existed: the old last-callsign guess is re-judged from the words
    assert speaker.best({"text": "K7ABC, thank you, W9XYZ", "callsigns": ["K7ABC", "W9XYZ"], "speaker": "W9XYZ"}) == (None, None)
    assert speaker.best({"text": "This is K7ABC", "callsigns": ["K7ABC"], "speaker": "K7ABC"}) == ("K7ABC", "stated")


def test_ics309_uses_the_best_speaker():
    base = {"radio": "a", "status": "final", "start": "2026-11-02T19:00:00", "hint_keys": []}
    span = [ics.view(dict(base, text="K7ABC, thank you, you're in the log.", callsigns=["K7ABC"],
                          speaker="K7NET", speaker_how="ncs")),
            ics.view(dict(base, text="Good evening.", callsigns=[], speaker=None, speaker_how=None, speaker_set="KA7SU"))]
    rows = ics.drafts(span, {}, {}, [], None, "T")[0]["rows"]
    assert (rows[0]["from"], rows[0]["to"]) == ("K7NET", "K7ABC") and rows[1]["from"] == "KA7SU"
    people = ics.participants(span, set(), {})
    assert {p["call"] for p in people} == {"K7ABC", "K7NET", "KA7SU"}        # an operator-set speaker took part


def test_with_who_marks_each_transmission():
    t = {"id": "1", "text": "This is K7ABC", "callsigns": ["K7ABC"], "speaker": "K7ABC", "speaker_how": "stated"}
    assert with_who("tx", t)["who"] == {"call": "K7ABC", "how": "stated"}
    assert with_who("snapshot", {"tx": [dict(t, speaker_set="W9XYZ")]})["tx"][0]["who"]["how"] == "operator"
    assert with_who("heard", {"heard": []}) == {"heard": []}


@pytest.fixture
def station(tmp_path):
    texts = ["Roger, you're in the log. Have a good evening."]
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(texts, "fake-final"), callbook=FakeBook({"K7ABC": "Pat"}),
                 data_dir=tmp_path)
    st.start(listen=False)
    yield st
    st.stop.set()


def test_operator_sets_the_speaker_and_it_survives(station, tmp_path):
    station.inject("ftm", np.zeros(16000 * 2, np.float32))
    assert wait_for(lambda: station.tx and station.tx[-1]["status"] == "final")
    tid = station.tx[-1]["id"]
    assert (station.tx[-1]["speaker"], station.tx[-1]["speaker_how"]) == ("K7NET", "ncs")   # schedule ncs = K7NET
    q = station.bus.subscribe()
    station.set_speaker(tid, " k7abc ")
    assert q.get(timeout=2)[1]["speaker_set"] == "K7ABC"
    station.apply_accurate(tid, {"text": "Roger, you're in the log.", "model": "large-v3"})
    assert speaker.best(station.tx[-1]) == ("K7ABC", "operator")                    # the accurate pass never overrides
    with pytest.raises(ValueError):
        station.set_speaker(tid, "<b>")
    with pytest.raises(LookupError):
        station.set_speaker("nope", "K7ABC")
    log = [json.loads(x) for x in next((tmp_path / "logs").glob("*.jsonl")).read_text().splitlines()]
    assert {"update": tid, "speaker_set": "K7ABC"} in log
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path)
    again.load_today()
    t = again._find(tid)
    assert t["speaker_set"] == "K7ABC" and t["accurate"]["model"] == "large-v3"   # both updates merged, neither lost
    station.set_speaker(tid, "")                                                     # back to automatic
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path)
    again.load_today()
    assert speaker.best(again._find(tid)) == ("K7NET", "ncs")


def test_who_endpoint(station):
    station.inject("ftm", np.zeros(16000 * 2, np.float32))
    assert wait_for(lambda: station.tx and station.tx[-1]["status"] == "final")
    tid = station.tx[-1]["id"]
    srv = serve(station, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda path, body, ctype="application/json": urllib.request.urlopen(urllib.request.Request(
        base + path, data=body, method="POST", headers={"Content-Type": ctype}))
    try:
        d = json.loads(post(f"/api/tx/{tid}/speaker", b'{"call": "W7XYZ"}').read())
        assert d["who"] == {"call": "W7XYZ", "how": "operator"}
        for path, body, ctype, code in [(f"/api/tx/{tid}/speaker", b'{"call": "x y"}', "application/json", 400),
                                        ("/api/tx/123/speaker", b'{"call": "W7XYZ"}', "application/json", 404),
                                        (f"/api/tx/{tid}/speaker", b"call=W7XYZ", "application/x-www-form-urlencoded", 404)]:
            with pytest.raises(urllib.error.HTTPError) as e:
                post(path, body, ctype)
            assert e.value.code == code
        s = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert s["tx"][-1]["who"]["call"] == "W7XYZ"
    finally:
        srv.shutdown()


def test_voice_backfill_needs_a_clear_match():
    rng = np.random.default_rng(0)
    unit = lambda v: v / np.linalg.norm(v)
    a, b = unit(rng.standard_normal(16)), unit(rng.standard_normal(16))
    near = lambda v, s: unit(v + s * rng.standard_normal(16))
    items = [{"id": "1", "emb": a, "call": "K7ABC"}, {"id": "2", "emb": b, "call": "W9XYZ"},
             {"id": "3", "emb": near(a, 0.1), "call": None},              # clearly K7ABC
             {"id": "4", "emb": unit(a + b), "call": None},               # halfway: no margin, no answer
             {"id": "5", "emb": unit(rng.standard_normal(16)), "call": None},   # nobody we know
             {"id": "6", "emb": None, "call": None}]
    out = speaker.backfill(items, threshold=0.6, margin=0.08)
    assert set(out) == {"3"} and out["3"]["call"] == "K7ABC" and out["3"]["sim"] > 0.9
    assert speaker.backfill([{"id": "1", "emb": a, "call": None}]) == {}         # no anchors, nothing inferred


def test_voiceprint_features():
    from station.voiceprint import _BANKS, fbank
    assert _BANKS.shape == (80, 257) and (_BANKS.sum(axis=1) > 0).all()
    t = np.arange(16000) / 16000
    f = fbank((0.3 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32))
    assert f.shape == (98, 80) and np.isfinite(f).all() and np.abs(f.mean(axis=0)).max() < 1e-3   # 25/10 ms, CMN
    loud = np.concatenate([0.001 * np.ones(8000), 0.3 * np.sin(2 * np.pi * 800 * t[:8000])]).astype(np.float32)
    assert len(fbank(loud, keep=0.5)) < len(fbank(loud))
    assert fbank(np.zeros(100, np.float32)).shape == (0, 80)
