"""Morse code (CW) on any radio: station/morse.py, and its rows in the transcript."""
import copy
import json
import time

import numpy as np
import pytest

from station import morse as M
from station.core import Station
from station import engine
from tests.test_station import CFG, FakeBook, station  # noqa: F401 — the fixture

SR = 16000


@pytest.mark.parametrize("text,wpm,tone,jitter,snr", [
    ("CQ CQ DE W9XYZ K", 20, 700, 0.0, None),          # clean (digital silence between elements)
    ("W9XYZ/R", 15, 600, 0.0, 10),                       # a repeater's CW identification
    ("TEST DE K7ABC <AR>", 25, 800, 0.1, 6),
    ("PARIS PARIS", 30, 650, 0.15, 3),                   # a hand-keyed fist in noise
    ("SOS", 12, 550, 0.1, 0),
    ("EEE TTT", 20, 700, 0.05, 10),                      # every element alike: the dit from the gaps
    ("5NN TU", 35, 750, 0.1, 10),
    ("ID 73", 8, 900, 0.1, 10),
])
def test_decodes_cw_across_speeds_tones_noise_and_fists(text, wpm, tone, jitter, snr):
    r = M.decode(M.synth(text, wpm, tone, jitter=jitter, snr_db=snr, seed=3))
    assert r and r["text"] == text
    assert abs(r["wpm"] - wpm) / wpm < 0.15 and abs(r["tone_hz"] - tone) < 20
    assert r["symbols"].split(" / ")[0].split()[0] == {v: k for k, v in M.CODE.items()}[text[0] if text[0] != "<" else text[:4]]


def test_not_morse():
    rnd = np.random.default_rng(0)
    t = np.arange(SR * 5) / SR
    tone = lambda f, n: 0.3 * np.sin(2 * np.pi * f * t[:n])
    for x in (rnd.standard_normal(SR * 5) * 0.1,                                         # hiss
              tone(700, SR * 5),                                                          # a steady carrier
              np.concatenate([np.zeros(8000), tone(1000, 3200), np.zeros(8000)]),         # a courtesy beep
              np.tile(np.concatenate([tone(697, 1600) + tone(1209, 1600), np.zeros(1600)]), 8),   # DTMF
              tone(100, SR * 5) * 0.7 + rnd.standard_normal(SR * 5) * 0.05,               # a sub-audible tone
              np.sin(2 * np.pi * np.cumsum(180 + 60 * np.sin(2 * np.pi * 3 * t)) / SR) *  # a wandering pitch, syllables
              (np.sin(2 * np.pi * 4 * t) > 0) * 0.3):
        assert M.decode(np.asarray(x, np.float32)) is None


def test_streaming_finds_the_run_and_its_time():
    rnd = np.random.default_rng(1)
    x = np.concatenate([rnd.standard_normal(2 * SR) * 0.01, M.synth("CQ DE W9XYZ", 20, 700, snr_db=15),
                        rnd.standard_normal(4 * SR) * 0.01]).astype(np.float32)
    w, out = M.MorseWatch(), []
    for i in range(0, len(x), 1600):
        out += w.feed(x[i:i + 1600], 1000 + i / SR)
    out += w.flush()
    assert [o["text"] for o in out] == ["CQ DE W9XYZ"]
    assert 1002.0 <= out[0]["t0"] <= 1002.5 and len(out[0]["audio"]) > SR


def _run(st, text, t0):
    x = M.synth(text, 20, 700, snr_db=15)
    r = M.decode(x, t0)
    r["audio"] = x
    st.morse_found("ftm", r)
    return r


def test_a_cw_run_is_a_transcript_row_and_never_goes_to_the_accurate_pass(station):  # noqa: F811
    _run(station, "DE W9XYZ K", time.time() - 5)
    t = station.tx[-1]
    assert t["kind"] == "cw" and t["text"] == "DE W9XYZ K" and t["status"] == "final" and t["wav"].endswith("_cw.wav")
    assert t["morse"][0]["symbols"].startswith("-.. .") and t["callsigns"] == ["W9XYZ"]
    assert (station.data / t["wav"]).exists()
    assert not [x for x in station.accurate._span("ftm", "2000-01-01T00:00:00", "2100-01-01T00:00:00") if x["id"] == t["id"]]
    line = json.loads(station._log_path().read_text(encoding="utf-8").splitlines()[-1])
    assert line["kind"] == "cw" and line["morse"][0]["text"] == "DE W9XYZ K"


def test_cw_heard_during_a_voice_transmission_is_attached_to_it_and_survives_a_restart(station, tmp_path):  # noqa: F811
    station.inject("ftm", np.zeros(SR * 3, np.float32))
    for _ in range(100):
        if station.tx[-1]["status"] == "final":
            break
        time.sleep(0.05)
    tx = station.tx[-1]
    from datetime import datetime
    a = datetime.fromisoformat(tx["start"]).timestamp()
    _run(station, "K7ABC", a + 0.5)
    assert station.tx[-1] is tx and tx["morse"][0]["text"] == "K7ABC" and tx.get("kind") != "cw"
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=station.data)
    again.load_today()
    t2 = next(t for t in again.tx if t["id"] == tx["id"])
    assert t2["morse"][0]["text"] == "K7ABC"


def test_morse_can_be_turned_off_per_radio(tmp_path):
    cfg = copy.deepcopy(CFG)
    cfg["radios"][0]["morse"] = False
    st = Station(cfg, lambda: engine.FakeEngine(), data_dir=tmp_path)
    assert cfg["radios"][0]["id"] not in st.morse
