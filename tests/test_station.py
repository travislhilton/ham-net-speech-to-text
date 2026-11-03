"""The live station end to end with a fake Whisper: segmenting, enrichment, log, server, schedule."""
import copy
import json
import threading
import time
import urllib.request
from datetime import datetime

import numpy as np
import pytest

from station import engine
from station.callbook import Callbook
from station.core import Station, speaker_of
from station.segmenter import Segmenter
from station.server import serve
from station.textclean import collapse_repeats, max_tokens


class ScriptGate:
    def __init__(self, pattern):
        self.p = list(pattern)

    def is_speech(self, _f):
        return self.p.pop(0) if self.p else False


def run(seg, n):
    out = []
    for _ in range(n):
        out += list(seg.feed(np.zeros(512, np.float32)))
    return out


def test_segmenter_start_partials_end_and_kerchunk():
    seg = Segmenter(ScriptGate([False] * 5 + [True] * 70 + [False] * 40), frame_ms=32, min_speech_ms=400,
                    hangover_ms=320, preroll_ms=96, partial_every_s=1.0, clock=lambda: 100.0)
    ev = run(seg, 115)
    kinds = [e[0] for e in ev]
    assert kinds[0] == "start" and kinds[-1] == "end" and kinds.count("audio") == 2
    audio, info = ev[-1][1], ev[-1][2]
    assert not info["kerchunk"] and info["speech_frames"] == 70 and len(audio) == (3 + 70 + 10) * 512
    blip = Segmenter(ScriptGate([True] * 3 + [False] * 20), hangover_ms=320, min_speech_ms=400)
    assert run(blip, 23)[-1][2]["kerchunk"]


def test_segmenter_splits_a_long_carrier_at_speech_pauses():
    """Repeater hang time: the gate never closes, so pieces are cut where the voice pauses (>= 0.8 s)."""
    from station.segmenter import Segmenter

    class Always:
        def is_speech(self, f):
            return True
    rng = np.random.default_rng(1)
    sr, n = 16000, 512
    def speech(s):   # 1 kHz tone with syllable-like bursts over a hiss floor
        t = np.arange(int(sr * s)) / sr
        return (0.3 * np.sin(2 * np.pi * 1000 * t) * (np.sin(2 * np.pi * 3 * t) > -0.3) + 0.003 * rng.standard_normal(len(t)))
    pause = lambda s: 0.003 * rng.standard_normal(int(sr * s))
    audio = np.concatenate([pause(6), speech(4), pause(1.5), speech(5), pause(1.5), speech(3), pause(0.3)]).astype(np.float32)
    seg = Segmenter(Always(), sr, 32, min_speech_ms=400, hangover_ms=900, split_pause_ms=800, min_split_s=2.0, clock=lambda: 0)
    ends = [ev for i in range(0, len(audio) - n + 1, n) for ev in seg.feed(audio[i:i + n]) if ev[0] == "end"]
    pieces = [e[2] for e in ends if not e[2]["kerchunk"]]
    assert len(pieces) == 3 - 1 and all(p["split"] for p in pieces)          # the third is still open (carrier up)
    assert 9 < pieces[0]["duration_s"] < 12 and 5 < pieces[1]["duration_s"] < 8   # 6 s quiet + 4 s, then 5 s + pause


def test_highpass_removes_tone_and_hum(station):
    t = np.arange(16000) / 16000
    hum = (0.2 * np.sin(2 * np.pi * 100 * t) + 0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    voice = (0.2 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
    rid = next(iter(station.radios))
    station.radio_cfg[rid] = dict(station.radio_cfg[rid], gate="carrier")
    out = station.clean(rid, hum + voice)[4000:]
    assert np.sqrt(np.mean(out ** 2)) < 0.16 and np.sqrt(np.mean(out ** 2)) > 0.12      # ~voice only (rms 0.141)
    station.radio_cfg[rid]["highpass_hz"] = 0
    assert station.clean(rid, hum) is hum


def test_probably_noise():
    from station.core import probably_noise
    assert probably_noise("So.", []) and probably_noise("We'll be right back.", []) and probably_noise(".", [])
    assert not probably_noise("Thank you, KC7EZ, good evening.", ["KC7EZ"]) and not probably_noise("No traffic tonight.", [])


def test_loop_guard():
    t, looped = collapse_repeats("Net control this is K7ABC. Thank you. Thank you. Thank you. Thank you. Over.")
    assert looped and t == "Net control this is K7ABC. Thank you. Over."
    assert collapse_repeats("I said no, no, no traffic")[0] == "I said no, traffic"
    assert collapse_repeats("Roger roger, clear") == ("Roger roger, clear", False)
    assert max_tokens(5) == 64 and max_tokens(600) == 440


def test_speaker_guess():
    assert speaker_of("Net control, this is W9XYZ checking in", ["W9XYZ"]) == "W9XYZ"
    assert speaker_of("K7ABC, W9XYZ, go ahead", ["K7ABC", "W9XYZ"]) == "W9XYZ"
    assert speaker_of("no calls", []) is None


class FakeBook(Callbook):
    def __init__(self, known):
        self.known_db, self.qrz, self.roster, self.known, self.con, self.cty = known, None, set(), {}, None, None
        import threading as t; self.lock = t.Lock()

    def fcc(self, call):
        return {"call": call, "name": self.known_db[call]} if call in self.known_db else None

    def validate(self, call):
        return call in self.known_db or call in self.roster

    def card(self, call, qrz_network=False):
        return {"call": call, "name": self.known_db.get(call), "sources": ["FCC"]}


CFG = {"callsign": "W9XYZ", "radios": [{"id": "ftm", "name": "FTM-150R", "device": "NET-REC", "gate": "carrier",
                                         "freq_mhz": 146.94}],
       "schedule": [{"name": "Test net", "radio": "ftm", "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                     "start": "00:00", "duration_min": 1439, "ncs": "K7NET", "freq_mhz": 146.82}],
       "port": 0, "bind": "127.0.0.1", "qrz_online": False}


def wait_for(cond, s=5):
    t = time.time()
    while time.time() - t < s:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def station(tmp_path):
    texts = ["Break break, emergency traffic. Net control, this is K-7-A-B-C, QTH Springfield. Over."]
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(texts, "fake-final"), lambda: engine.FakeEngine(name="fake-partial"),
                 callbook=FakeBook({"K7ABC": "Pat Example", "W9XYZ": "Pat Q Sample"}), data_dir=tmp_path)
    st.start(listen=False)
    yield st
    st.stop.set()


def test_transmission_flow_enrichment_and_log(station, tmp_path):
    q = station.bus.subscribe()
    station.inject("ftm", np.zeros(16000 * 3, np.float32))
    assert wait_for(lambda: station.tx and station.tx[-1]["status"] == "final")
    tx = station.tx[-1]
    assert tx["callsigns"] == ["K7ABC"] and tx["speaker"] == "K7ABC" and tx["model"] == "fake-final"
    assert {t["term"] for t in tx["terms"]} >= {"QTH", "BREAK BREAK"}
    assert station.hints["ftm"]["net"]["type"] == "emergency" and station.hints["ftm"]["cards"][0]["key"] == "emergency"
    assert station.heard["K7ABC"]["card"]["name"] == "Pat Example"
    kinds = set()
    while not q.empty():
        kinds.add(q.get()[0])
    assert {"tx", "hints", "heard"} <= kinds
    log = list((tmp_path / "logs").glob("*.jsonl"))[0].read_text().splitlines()
    assert json.loads(log[-1])["callsigns"] == ["K7ABC"]
    assert (tmp_path / tx["wav"]).exists()
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    again.load_today()
    assert again.tx[-1]["id"] == tx["id"] and again.heard["K7ABC"]["count"] == 1
    assert again.hints["ftm"]["net"]["type"] == "emergency"


def test_schedule_current_and_retune(station):
    n = station.schedule_now(datetime(2026, 11, 3, 12, 0))
    cur = n["current"]
    assert cur["name"] == "Test net" and cur["retune"] and cur["band"]["band"] == "2 m" and cur["ncs"] == "K7NET"


def test_server_state_events_audio_lookup(station):
    srv = serve(station, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        station.inject("ftm", np.zeros(16000 * 2, np.float32))
        assert wait_for(lambda: station.tx and station.tx[-1]["status"] == "final")
        s = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert s["callsign"] == "W9XYZ" and s["radios"][0]["band"]["band"] == "2 m" and s["tx"]
        assert b"HamNet Station" in urllib.request.urlopen(base + "/").read()
        wav = urllib.request.urlopen(f"{base}/audio/{station.tx[-1]['id']}").read()
        assert wav[:4] == b"RIFF"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/audio/../../etc")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/lookup?call=%3Cscript%3E")
        r = urllib.request.urlopen(base + "/events", timeout=5)
        assert r.readline().strip() == b"event: snapshot"
        r.close()
    finally:
        srv.shutdown()


def test_callbook_validate_rules(tmp_path):
    cb = Callbook(fcc_db=tmp_path / "none.sqlite", cty=tmp_path / "none.dat")
    cb.fcc = lambda c: {"call": c} if c == "W9XYZ" else None
    cb.con = object()
    cb.country = lambda c: {"country": "Canary Islands"} if c.startswith("EA8") else (
        {"country": "United States"} if c[0] in "KNW" else None)
    assert cb.validate("W9XYZ") and not cb.validate("W9THQ")          # US: must be licensed
    assert cb.validate("EA8AB") and not cb.validate("ZZ1")            # foreign: a country + a full call
    cb.roster.add("N0NET"); cb.known.clear()
    assert cb.validate("N0NET")


def test_operator_sets_frequency_persisted_and_stamped(station, tmp_path):
    r = station.set_radio("ftm", "145.900", "fm")
    assert r["freq_mhz"] == 145.9 and r["mode"] == "FM" and r["band"]["band"] == "2 m"
    with pytest.raises(ValueError):
        station.set_radio("ftm", 99999)
    with pytest.raises(ValueError):
        station.set_radio("ftm", 145.9, "XYZ")
    with pytest.raises(KeyError):
        station.set_radio("nope", 145.9)
    station.inject("ftm", np.zeros(16000 * 2, np.float32))
    assert wait_for(lambda: station.tx and station.tx[-1]["status"] == "final")
    assert station.tx[-1]["freq_mhz"] == 145.9
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path)       # restart keeps it
    assert again.radios["ftm"]["freq_mhz"] == 145.9 and again.radios["ftm"]["tuned_at"]


def test_post_radio_requires_json(station):
    srv = serve(station, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/radios/ftm"
    try:
        req = urllib.request.Request(url, data=b'{"freq_mhz": 146.52, "mode": "FM"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["freq_mhz"] == 146.52
        form = urllib.request.Request(url, data=b"freq_mhz=1", method="POST",
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(form)
        bad = urllib.request.Request(url, data=b'{"freq_mhz": "abc"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 400
    finally:
        srv.shutdown()


def test_short_prompt_carries_frequency_and_phonetic_call(station):
    p = station.prompt_for("ftm")
    assert p == "Ham radio net on 146.940 MHz. This is W9XYZ, Whiskey Nine X-ray Yankee Zulu. Over." and len(p) < 120
    station.set_radio("ftm", 145.9)
    assert "145.900 MHz" in station.prompt_for("ftm")


def test_raw_ring_keeps_the_last_minutes(tmp_path):
    from station.core import RawRing
    r = RawRing(tmp_path / "raw", keep_min=2)
    for i in range(4):
        r.buf, r.t0 = [np.zeros(512, np.float32)], datetime(2026, 11, 2, 16, i)
        r.flush()
    assert sorted(p.name for p in (tmp_path / "raw").glob("*.wav")) == ["20261102-160200.wav", "20261102-160300.wav"]


def test_pause_and_clear_survive_a_restart(station, tmp_path):
    station.inject("ftm", np.zeros(16000 * 3, np.float32))
    assert wait_for(lambda: station.tx and station.tx[-1]["status"] == "final")
    station.set_radio("ftm", 146.94, "FM")
    assert station.set_paused("ftm", True)["paused"] is True
    time.sleep(1.1)                                   # cleared_at is to the second; the log line is earlier
    r = station.clear_radio("ftm")
    assert not [t for t in station.tx if t["radio"] == "ftm"] and "K7ABC" not in station.heard
    assert list((tmp_path / "logs").glob("*.jsonl"))[0].read_text().strip()          # files kept on disk
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    again.load_today()
    assert again.radios["ftm"]["paused"] and again.radios["ftm"]["cleared_at"] == r["cleared_at"]
    assert again.radios["ftm"]["freq_mhz"] == 146.94                                # tuning kept beside pause/clear
    assert not again.tx and not again.heard                                          # cleared stays cleared
    assert station.set_paused("ftm", False)["paused"] is False


def test_segmenter_flush_on_pause():
    from station.segmenter import Segmenter

    class Always:
        def is_speech(self, f):
            return True
    seg = Segmenter(Always(), 16000, 32, clock=lambda: 0)
    for _ in range(40):
        list(seg.feed(np.full(512, 0.1, np.float32)))
    ev = list(seg.flush())
    assert ev[0][0] == "end" and ev[0][2]["paused"] and not seg.active and list(seg.flush()) == []


class FakeRemote:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, 0

    def reachable(self):
        return self.ok

    def transcribe(self, wav, prompt, highpass_hz):
        self.calls += 1
        assert wav[:4] == b"RIFF" and "W9XYZ" in prompt and highpass_hz == 300
        return {"text": "Net control, this is K7ABC in Springfield, no traffic.", "model": "fake-large", "audio_s": 3.0, "elapsed_s": 0.1}


def test_accurate_pass_beside_the_live_text(tmp_path):
    cfg = copy.deepcopy(CFG); cfg["accurate"] = {"auto_after_min": 0}
    st = Station(cfg, lambda: engine.FakeEngine(["Net control this is K7-ABC, over."]), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    st._accurate_remote = FakeRemote()
    st.start(listen=False)
    try:
        q = st.bus.subscribe()
        st.inject("ftm", np.zeros(16000 * 3, np.float32))
        assert wait_for(lambda: st.tx and st.tx[-1]["status"] == "final")
        live = st.tx[-1]["text"]
        j = st.accurate_now("ftm")
        assert j["total"] == 1 and j["state"] in ("queued", "running", "done")
        assert wait_for(lambda: st.accurate.status()[0]["state"] == "done")
        tx = st.tx[-1]
        assert tx["text"] == live and tx["accurate"]["text"].startswith("Net control, this is K7ABC")   # live kept
        assert tx["accurate"]["model"] == "fake-large" and st.accurate.status()[0]["done"] == 1
        p = st.winlink_preview("ftm")
        assert p["drafts"][0]["rows"][0]["sub"].startswith("Net control, this is K7ABC in Springfield") and p["accurate_missing"] == 0
        assert p["accurate"]["state"] == "done"
        kinds = set()
        while not q.empty():
            kinds.add(q.get()[0])
        assert "accurate" in kinds
        assert st.accurate_now("ftm")["state"] == "done" and st._accurate_remote.calls == 1   # nothing left to do
        again = Station(copy.deepcopy(cfg), lambda: engine.FakeEngine(), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
        again.load_today()
        assert again.tx[-1]["accurate"]["text"] == tx["accurate"]["text"]                    # survives a restart
    finally:
        st.stop.set()


def test_accurate_pass_unreachable_and_auto(tmp_path):
    from datetime import datetime, timedelta
    cfg = copy.deepcopy(CFG); cfg["accurate"] = {"auto_after_min": 15, "live": False}
    st = Station(cfg, lambda: engine.FakeEngine(["Testing, this is W9XYZ."]), callbook=FakeBook({}), data_dir=tmp_path)
    st._accurate_remote = FakeRemote(ok=False)
    st.start(listen=False)
    try:
        st.inject("ftm", np.zeros(16000 * 2, np.float32))
        assert wait_for(lambda: st.tx and st.tx[-1]["status"] == "final")
        st.accurate.tick(datetime.now())                                   # not over yet: nothing queued
        assert st.accurate.status() == []
        st.accurate.tick(datetime.now() + timedelta(minutes=16))           # 16 quiet minutes: queued by itself
        assert wait_for(lambda: st.accurate.status() and st.accurate.status()[0]["state"] == "unreachable")
        assert st.accurate.status()[0]["auto"] and not st.tx[-1].get("accurate")
    finally:
        st.stop.set()


def test_wer_compares_numbers_and_callsigns():
    from station.wer import TEST_TEXT, wer, words
    assert words("145.9 W9XYZ") == words("one four five point nine W 9 T L H")
    assert wer(TEST_TEXT, TEST_TEXT, "W9XYZ")["wer"] == 0
    r = wer(TEST_TEXT, "This is W9XOZ testing speech to text on 145.9. The quick brown fox jumps over the lazy dog.", "W9XYZ")
    assert 0.2 < r["wer"] < 0.4 and r["words"] == len(words(TEST_TEXT, "W9XYZ"))


def test_score_test_and_history(tmp_path):
    from station.wer import TEST_TEXT
    cfg = copy.deepcopy(CFG); cfg["accurate"] = {"auto_after_min": 0, "live": False}     # the score-first path
    st = Station(cfg, lambda: engine.FakeEngine([TEST_TEXT.replace("lazy", "lacy")]), callbook=FakeBook({"W9XYZ": "T"}), data_dir=tmp_path)
    st._accurate_remote = FakeRemote()
    with pytest.raises(ValueError):
        st.score_test("ftm")
    st.start(listen=False)
    try:
        st.inject("ftm", np.zeros(16000 * 3, np.float32))
        assert wait_for(lambda: st.tx and st.tx[-1]["status"] == "final")
        r = st.score_test("ftm")
        assert 0 < r["live"]["wer"] < 0.1 and r["accurate_pending"]
        assert wait_for(lambda: st.tx[-1].get("accurate"))
        r2 = st.score_test("ftm", st.tx[-1]["id"])
        assert "accurate" in r2 and r2["accurate"]["wer"] > 0.5            # the fake Mac text is a different sentence
        h = st.test_history()
        assert len(h) == 1 and "accurate" in h[0]                          # the re-score replaced the first
    finally:
        st.stop.set()


def test_accurate_pass_follows_each_transmission(tmp_path):
    cfg = copy.deepcopy(CFG); cfg["accurate"] = {"auto_after_min": 0}           # live is the default
    st = Station(cfg, lambda: engine.FakeEngine(["Net control this is K7-ABC, over.", "W9XYZ, go ahead."]),
                 callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    st._accurate_remote = FakeRemote()
    st.start(listen=False)
    try:
        for _ in range(2):
            st.inject("ftm", np.zeros(16000 * 3, np.float32))
            assert wait_for(lambda: st.tx and st.tx[-1]["status"] == "final" and st.tx[-1].get("accurate"))
        jobs = st.accurate.status()
        assert len(jobs) == 1 and jobs[0]["state"] == "done" and jobs[0]["done"] == jobs[0]["total"] == 2   # one job per net
        assert st._accurate_remote.calls == 2
    finally:
        st.stop.set()
