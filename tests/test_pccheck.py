"""This PC's speed: measured at full speed, power settings checked, slow transcription noticed."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from station import pccheck


def test_the_checks_name_each_problem_and_its_fix():
    f = {"on_battery": True, "battery": True, "mode_ac": pccheck.BEST_EFFICIENCY, "hibernate_ac_s": 3600,
         "modern_standby": True, "maker": "ASUSTeK COMPUTER INC.",
         "gpus": [{"name": "Intel(R) Graphics", "date": "2024-01-02"}]}
    got = {c["id"]: c for c in pccheck.checks(f, {"perf_held": 25, "slowed": True}, today=date(2026, 11, 12))}
    assert not got["battery"]["ok"] and not got["mode"]["ok"] and not got["hibernate"]["ok"]
    assert got["standby"]["ok"] and "keeps the screen on" in got["standby"]["fix"]
    assert not got["driver"]["ok"] and "2024-01-02" in got["driver"]["say"]
    assert "held_back" not in got                       # on battery: being held back is expected
    assert not got["measured_slowed"]["ok"]
    f["on_battery"] = False
    got = {c["id"]: c for c in pccheck.checks(f, {"perf_held": 25}, today=date(2026, 11, 12))}
    assert got["battery"]["ok"] and "MyASUS" in got["held_back"]["fix"]


def test_a_good_pc_has_nothing_to_fix():
    f = {"battery": True, "on_battery": False, "mode_ac": pccheck.BEST_PERFORMANCE, "hibernate_ac_s": 0,
         "gpus": [{"name": "Intel(R) Graphics", "date": "2026-09-01"}]}
    assert all(c["ok"] for c in pccheck.checks(f, {"perf_held": 180}, today=date(2026, 11, 12)))


class Hold:
    def __init__(self):
        self.calls = []

    def set(self, v):
        self.calls.append(v)


def test_measure_holds_full_speed_waits_for_it_and_always_lets_go(tmp_path):
    speeds = iter([25, 30, 90, 150])
    ran, hold, envs = [], Hold(), []

    def fake(a, check=False, env=None):
        ran.append(a[1]); envs.append(env["HAMNET_PLAN_HELD"])
    out = tmp_path / "engine.yaml"
    out.write_text("final: {device: GPU, model: m, rtf: 0.03}\naccurate: {mode: final_only, rtf: 0.3}\n")
    r = pccheck.measure(tmp_path, tmp_path, out, py="py", run=fake,
                        hold=hold, pct=lambda *a: next(speeds), sleep=lambda s: None, say=lambda s: None)
    assert hold.calls == [True, False] and [p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for p in ran] == \
        ["pick_engine.py", "pick_accurate.py"]
    assert envs == ["1", "1"]
    assert r["perf_before"] == 25 and r["perf_held"] == 90 and not r["slowed"] and r["final"]["rtf"] == 0.03
    assert pccheck.load(tmp_path)["measured"]["perf_held"] == 90


def test_a_measurement_that_stayed_slow_is_marked(tmp_path, monkeypatch):
    monkeypatch.setattr(pccheck, "WAIT_S", 0)
    hold = Hold()
    r = pccheck.measure(tmp_path, tmp_path, tmp_path / "e.yaml", run=lambda a, check=False, env=None: None, hold=hold,
                        pct=lambda *a: 25, sleep=lambda s: None, say=lambda s: None)
    assert r["slowed"] and hold.calls == [True, False]


def test_the_choice_is_asked_once_and_saved_where_settings_reads_it(tmp_path):
    assert not pccheck.asked(tmp_path)
    pccheck.consent(tmp_path, "listening")
    assert pccheck.asked(tmp_path)
    import json
    assert json.loads((tmp_path / "operator.json").read_text())["operator"]["keep_awake"] == "listening"
    assert "whole time a radio is listening" in " ".join(pccheck.explain("listening"))
    assert "changes no power settings" in pccheck.explain("off")[0]


def test_slow_transcription_is_noticed_and_clears():
    now = [datetime(2026, 11, 12, 20, 0)]
    w = pccheck.SlowWatch(0.03, clock=lambda: now[0])
    for _ in range(3):
        w.add(1.0, 10.0)                                  # 0.10 s per s: fine (under the 0.5 floor)
    assert w.state() is None
    for _ in range(3):
        s = w.add(6.0, 10.0, "net")                       # 0.6 s per s: 20x what it measured
    assert s and s["rtf"] == 0.6 and "screen off" in s["say"]
    for _ in range(3):
        w.add(1.0, 10.0)
    assert w.state() is None
    for _ in range(3):
        w.add(6.0, 10.0)
    now[0] += timedelta(minutes=11)                       # nothing more heard: it lapses
    assert w.state() is None
    assert pccheck.SlowWatch(None).add(9, 10) is None     # never measured: nothing to compare


def test_perf_reads_typeperf():
    out = ('"(PDH-CSV 4.0)","\\\\PC\\Processor Information(_Total)\\% Processor Performance"\n'
           '"11/12/2026 07:40:01.000","24.9"\n"11/12/2026 07:40:02.000","187.3"\n')
    assert pccheck.perf_pct(run=lambda *a, **k: SimpleNamespace(stdout=out)) == 187
