"""capture/gates.CarrierGate: hiss = closed, a quieted carrier with voice = open."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "capture"))
from gates import CarrierGate  # noqa: E402

SR, N = 16000, 512
rng = np.random.default_rng(1)


def hiss(n):  # open-squelch discriminator noise, roughly flat, ~-25 dBFS rms
    return (rng.standard_normal(n) * 0.056).astype(np.float32)


def carrier_voice(n):  # quieted carrier: voice-band tone plus hiss ~40 dB down
    t = np.arange(n) / SR
    return (0.05 * np.sin(2 * np.pi * 700 * t) + rng.standard_normal(n) * 0.0006).astype(np.float32)


def run(gate, signal):
    return [gate.is_speech(signal[i:i + N]) for i in range(0, len(signal) - N + 1, N)]


def test_hiss_stays_closed_and_carrier_opens_then_closes():
    g = CarrierGate({}, SR)
    sig = np.concatenate([hiss(SR * 2), carrier_voice(SR * 2), hiss(SR * 2)])
    out = run(g, sig)
    third = len(out) // 3
    assert not any(out[:third])                       # 2 s of hiss: never opens
    assert all(out[third + 1:2 * third - 1])          # carrier: open (allow one edge frame)
    assert not any(out[2 * third + 1:])               # hiss again: closed


def test_floor_follows_a_louder_receiver_without_opening():
    g = CarrierGate({}, SR)
    run(g, hiss(SR))
    louder = hiss(SR * 2) * 3                           # +9.5 dB hiss (volume/level change)
    assert not any(run(g, louder))


def test_a_small_dip_does_not_open():
    g = CarrierGate({"open_db": 15}, SR)
    run(g, hiss(SR))
    assert not any(run(g, hiss(SR) * 0.3))              # -10 dB dip: below the 15 dB threshold
