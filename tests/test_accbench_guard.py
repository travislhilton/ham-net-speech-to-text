"""tools/accbench/guard.py: a benchmark on a live station PC yields to the station and ends itself before memory runs out."""
import os

import pytest

from tools.accbench.guard import Guard


def make(tmp_path, free, busy=lambda url: None, **kw):
    log, died = [], []
    g = Guard(need_mb=4000, lock_dir=tmp_path, free=free, busy=busy, sleep=lambda s: None, die=lambda: died.append(1),
              log=log.append, **kw)
    return g, log, died


def test_waits_for_memory_before_loading_and_one_benchmark_at_a_time(tmp_path):
    seq = iter([(5000, 9000), (11000, 12000)])
    g, log, _ = make(tmp_path, lambda: next(seq))
    g.start(watchdog=False)
    assert "waiting for 10000 MB" in log[0] and g.waited_s == 30
    other, _, _ = make(tmp_path, lambda: (20000, 20000))
    with pytest.raises(SystemExit, match="one at a time"):
        other.start(watchdog=False)
    g.close()
    assert not (tmp_path / "accbench.lock").exists()


def test_a_stale_lock_from_a_dead_benchmark_does_not_block(tmp_path):
    (tmp_path / "accbench.lock").write_text("999999")
    g, _, _ = make(tmp_path, lambda: (20000, 20000))
    g.start(watchdog=False)
    assert (tmp_path / "accbench.lock").read_text() == str(os.getpid())
    g.close()


def test_yields_to_the_station_and_to_low_memory_between_windows(tmp_path):
    states = iter(["receiving", "its accurate pass is running", None, None])
    mem = iter([(20000, 20000), (20000, 20000), (3000, 20000), (9000, 20000)])
    g, _, _ = make(tmp_path, lambda: next(mem), busy=lambda url: next(states))
    g.pause()
    assert g.waited_s == 6                                   # two waits for the station, one for memory


def test_watchdog_ends_the_benchmark_when_memory_runs_short(tmp_path):
    g, log, died = make(tmp_path, lambda: (8000, 2000))       # commit headroom is what ran out
    g.lock.write_text(str(os.getpid()))
    assert g.check() is False and died and "ending the benchmark" in log[-1]
    assert not g.lock.exists()
    g2, _, died2 = make(tmp_path, lambda: (8000, 8000))
    assert g2.check() and not died2
