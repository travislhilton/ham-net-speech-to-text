"""A radio that went silent (station/silence.py): measured levels from a laptop station, 2026-11-06."""
import random

from station.silence import SilenceWatch


def run(w, t0, secs, level, rx=False, wander=2.0, seed=1):
    rnd = random.Random(seed)
    for s in range(secs):
        d = level + rnd.uniform(-wander, wander)
        for k in range(10):
            w.feed(t0 + s + k / 10, d, rx)
    return t0 + secs


def test_a_handheld_that_switched_off_is_told_from_a_closed_squelch():
    w = SilenceWatch("voice")
    t = run(w, 1000.0, 900, -82.0, wander=3.0)               # 15 min squelch closed, radio on: -79..-85
    assert w.state(t) is None
    t = run(w, t, 60, -87.5, wander=0.4)                      # off for 1 min: not yet
    assert w.state(t) is None
    t = run(w, t, 70, -87.5, wander=0.4)                      # off for 2+ min: dead flat, 5 dB under its own floor
    s = w.state(t)
    assert s["kind"] == "off" and "OFF" in s["say"] and t - 140 <= s["since"] <= t - 110
    t = run(w, t, 5, -25.0, rx=True, wander=3)                # it hears a transmission again
    t = run(w, t, 30, -82.0, wander=3.0)
    assert w.state(t) is None


def test_no_off_warning_before_the_floor_is_learned_or_on_a_quiet_but_live_radio():
    w = SilenceWatch("voice")
    t = run(w, 0.0, 300, -87.5, wander=0.4)                   # just started, already silent: no yardstick yet
    assert w.state(t) is None
    w = SilenceWatch("voice")
    t = run(w, 0.0, 3000, -82.0, wander=3.0)                  # 50 quiet minutes, radio on
    assert w.state(t) is None


def test_a_data_jack_with_no_hiss_is_dead():
    w = SilenceWatch("carrier")
    t = run(w, 0.0, 600, -30.0)                                # data jack hiss
    assert w.state(t) is None
    t = run(w, t, 125, -90.0, wander=0.5)
    assert w.state(t)["kind"] == "dead"


def test_quiet_only_during_a_net():
    w = SilenceWatch("voice")
    t = run(w, 0.0, 5, -25.0, rx=True)
    t = run(w, t, 11 * 60, -82.0, wander=3.0)
    assert w.state(t) is None and w.state(t, net_on=True)["kind"] == "quiet"
