"""The live station end to end with a fake Whisper: segmenting, enrichment, log, server, schedule."""
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
    st = Station(CFG, lambda: engine.FakeEngine(texts, "fake-final"), lambda: engine.FakeEngine(name="fake-partial"),
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
    assert station.hints["net"]["type"] == "emergency" and station.hints["cards"][0]["key"] == "emergency"
    assert station.heard["K7ABC"]["card"]["name"] == "Pat Example"
    kinds = set()
    while not q.empty():
        kinds.add(q.get()[0])
    assert {"tx", "hints", "heard"} <= kinds
    log = list((tmp_path / "logs").glob("*.jsonl"))[0].read_text().splitlines()
    assert json.loads(log[-1])["callsigns"] == ["K7ABC"]
    assert (tmp_path / tx["wav"]).exists()
    again = Station(CFG, lambda: engine.FakeEngine(), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    again.load_today()
    assert again.tx[-1]["id"] == tx["id"] and again.heard["K7ABC"]["count"] == 1
    assert again.hints["net"]["type"] == "emergency"


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
