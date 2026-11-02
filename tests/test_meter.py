"""Meter readings, colour states, and several radio streams running at once."""
import base64
import time

import numpy as np
import soundfile as sf

from capture.gates import CarrierGate
from station import engine
from station.core import Station
from station.meter import Meter, level_state, snr_state, DEFAULTS

SR = 16000
rng = np.random.default_rng(1)


def hiss(s, db=-25):
    return (rng.standard_normal(int(SR * s)) * 10 ** (db / 20)).astype(np.float32)


def carrier_with_voice(s, voice_db=-20, resid_db=-70):
    t = np.arange(int(SR * s)) / SR
    return (np.sin(2 * np.pi * 800 * t) * 10 ** (voice_db / 20) * np.sqrt(2)
            + rng.standard_normal(len(t)) * 10 ** (resid_db / 20)).astype(np.float32)


def readings(meter, audio, gate=None):
    out = []
    for i in range(0, len(audio) - 512, 512):
        f = audio[i:i + 512]
        rx = gate.is_speech(f) if gate else False
        r = meter.feed(f, rx, gate)
        if r:
            out.append(r)
    return out


def test_states():
    c = DEFAULTS
    assert level_state(-25, -15, c) == "ok"
    assert level_state(-50, -40, c) == "warn" and level_state(-20, -3, c) == "warn"
    assert level_state(-20, -0.5, c) == "bad" and level_state(-70, -65, c) == "bad"
    assert snr_state(30, True, c) == "ok" and snr_state(15, True, c) == "warn" and snr_state(5, True, c) == "bad"
    assert snr_state(30, False, c) == "idle"


def test_fm_quieting_snr_and_waterfall():
    g, m = CarrierGate({}, SR), Meter({}, SR, "carrier")
    rs = readings(m, np.concatenate([hiss(2), carrier_with_voice(2), hiss(1)]), g)
    idle, rx = rs[3], [r for r in rs if r["rx"]]
    assert idle["ls"] == "ok" and idle["ss"] == "idle" and abs(idle["rms"] + 25) < 1.5
    assert rx and min(r["snr"] for r in rx[2:]) > 30 and rx[3]["ss"] == "ok"
    row = np.frombuffer(base64.b64decode(rx[3]["s"]), np.uint8)
    assert len(row) == 128 and row.argmax() == int(800 / 8000 * 128)       # the 800 Hz tone's column
    assert m.limits()["span_hz"] == [0, 8000]


def test_voice_gate_snr_from_noise_floor():
    m = Meter({}, SR, "voice")
    rs = readings(m, hiss(3, -50))
    t = np.arange(SR) / SR
    loud = (hiss(1, -50) + np.sin(2 * np.pi * 1000 * t) * 0.1).astype(np.float32)
    r = [m.feed(loud[i:i + 512], True) for i in range(0, SR - 512, 512)]
    r = [x for x in r if x]
    assert rs[-1]["snr"] == 0.0 and r[-1]["snr"] > 20 and m.limits()["span_hz"] == [0, 4000]


def test_two_streams_at_once(tmp_path):
    for name in ("a", "b"):
        sf.write(tmp_path / f"{name}.wav", np.concatenate([hiss(1.5), carrier_with_voice(1.5), hiss(1.5)]), SR)
    cfg = {"callsign": "W9XYZ", "port": 0, "bind": "127.0.0.1", "qrz_online": False, "schedule": [],
           "radios": [{"id": "a", "name": "Radio A", "device": f"file:{tmp_path / 'a.wav'}", "gate": "carrier", "freq_mhz": 145.9},
                      {"id": "b", "name": "Radio B", "device": f"file:{tmp_path / 'b.wav'}", "gate": "carrier", "freq_mhz": 446.0}]}
    st = Station(cfg, lambda: engine.FakeEngine(name="fake"), data_dir=tmp_path / "d")
    q = st.bus.subscribe()
    st.start(listen=True)
    seen, deadline = {"a": 0, "b": 0}, time.time() + 8
    try:
        while time.time() < deadline and not (all(seen.values()) and {t["radio"] for t in st.tx if t["status"] == "final"} >= {"a", "b"}):
            try:
                kind, d = q.get(timeout=0.2)
            except Exception:  # noqa: BLE001
                continue
            if kind == "m":
                seen[d["id"]] += 1
        finals = {t["radio"] for t in st.tx if t["status"] == "final"}
        assert seen["a"] > 10 and seen["b"] > 10 and finals == {"a", "b"}
        assert st.radios["a"]["state"] == st.radios["b"]["state"] == "listening"
        assert {t["freq_mhz"] for t in st.tx} == {145.9, 446.0}
    finally:
        st.stop.set()
