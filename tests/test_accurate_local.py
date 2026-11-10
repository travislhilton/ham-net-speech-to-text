"""The accurate pass on the station PC itself (station/accurate_local.py, transcribe/decoders.py): the OpenVINO result
mapped to segments, Whisper's 30 s walk done one window at a time (so the pass can stop between windows for the live
text), the merge without word probabilities, the benchmark's decision, the child process protocol, and AccuratePass
choosing the home server when it answers and this PC otherwise."""
import copy
import io
import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import numpy as np

from station import accurate_local as al
from station import engine
from station.accurate import stage_of
from station.core import Station
from tests.test_accurate_stages import ContextRemote
from tests.test_station import CFG, FakeBook, wait_for
from transcribe import context as cx
from transcribe import decoders as dc

SR = 16000


def tones(spec, total_s):
    """Bursts of tone at [(start_s, end_s)] in silence."""
    x = np.zeros(int(total_s * SR), np.float32)
    for a, b in spec:
        t = np.arange(int(a * SR), int(b * SR))
        x[t] = 0.3 * np.sin(2 * np.pi * 800 * t / SR)
    return x


# ----- transcribe/decoders.py -----
def test_ov_segments_puts_words_in_their_chunk_and_closes_an_open_one():
    chunks = [NS(start_ts=0.0, end_ts=4.0, text=" This is W9XYZ."), NS(start_ts=4.5, end_ts=-1.0, text=" Radio")]
    words = [NS(word=" This", start_ts=0.1, end_ts=0.4), NS(word=" is", start_ts=0.4, end_ts=0.6),
             NS(word=" W9XYZ.", start_ts=0.6, end_ts=1.5), NS(word=" Radio", start_ts=4.6, end_ts=5.0)]
    segs = dc.ov_segments(chunks, words, dur=30.0)
    assert [len(s["words"]) for s in segs] == [3, 1] and segs[1]["end"] == 30.0     # no end -> the window's end
    assert all(w["probability"] is None for s in segs for w in s["words"])           # OpenVINO gives none
    assert dc.ov_segments([], words[:1], 3.0)[0]["text"] == " This"                   # words without chunks


def test_sequential_walks_30_s_windows_and_redoes_a_cut_last_segment():
    calls, paused = [], []

    def window(piece):
        calls.append(len(piece) / SR)
        d = len(piece) / SR
        if d >= 29.9:     # a full window: one finished segment, then one that runs into the window's end
            return [{"start": 0.0, "end": 20.0, "text": " a", "words": [{"word": " a", "start": 1.0, "end": 2.0}]},
                    {"start": 20.0, "end": 30.0, "text": " cut", "words": [{"word": " cut", "start": 21.0, "end": 29.9}]}]
        return [{"start": 0.0, "end": d, "text": " end", "words": [{"word": " end", "start": 0.5, "end": 1.0}]}]
    out = dc.sequential(np.zeros(SR * 45, np.float32), window, pause=lambda: paused.append(1))
    # 0-30 s keeps 'a' and drops 'cut'; the next window starts at 20 s (where 'cut' began) and runs to the end
    assert calls == [30.0, 25.0] and len(paused) == 2
    assert [(s["text"], s["start"]) for s in out] == [(" a", 0.0), (" end", 20.0)]
    assert out[1]["words"][0]["start"] == 20.5


def test_with_fallback_retries_a_looping_window_at_a_higher_temperature():
    temps = []

    def window(x, prompt, t):
        temps.append(t)
        text = " seven seven seven seven seven seven" if t < 0.4 else " K7ABC checking in"
        return [{"start": 0, "end": 1, "text": text, "words": []}]
    segs = dc.with_fallback(window)(np.zeros(SR, np.float32))
    assert temps == [0.0, 0.2, 0.4] and segs[0]["text"] == " K7ABC checking in"


def test_fake_decoder_through_the_context_method_without_probabilities():
    clips = [cx.Clip("1", 100.0, tones([(0.2, 0.6), (1.0, 1.4)], 2.0)), cx.Clip("2", 105.0, tones([(0.3, 0.8)], 1.5))]
    out = cx.transcribe_clips(clips, dc.make({"engine": "fake", "model": "f", "device": "CPU"}), mode="both")
    assert out["1"]["text"] == "w0 w1" and out["2"]["text"] == "w2"
    assert out["1"]["conf"] is None and out["1"]["words"][0][3] is None and "low_conf" not in out["1"]["flags"]


# ----- transcribe/context.py without word probabilities -----
def _seg(words):
    return [{"text": "".join(w[0] for w in words), "words": [{"word": w, "start": a, "end": b, "probability": p}
                                                             for w, a, b, p in words]}]


def test_no_probabilities_merge_by_window_edge_and_a_lone_noise_phrase_is_set_aside():
    lay = cx.stitch([cx.Clip("1", 0.0, np.zeros(SR * 10, np.float32)), cx.Clip("2", 20.0, np.zeros(SR * 2, np.float32))])
    long_ = [{"a": 0.0, "b": 12.5, "words": [
        {"w": " K7ABC", "t0": 1.0, "t1": 1.5, "p": None, "edge": 99.0, "ok": True, "why": ""},
        {"w": " Thank you.", "t0": 10.8, "t1": 11.5, "p": None, "edge": 99.0, "ok": True, "why": ""}]}]
    wins = [{"a": 0.0, "b": 12.5, "words": [
        {"w": " K7ABD", "t0": 1.0, "t1": 1.5, "p": None, "edge": 0.5, "ok": True, "why": ""},      # next to a cut
        {"w": " Thank you.", "t0": 10.8, "t1": 11.5, "p": None, "edge": 99.0, "ok": True, "why": ""}]}]
    res = cx.assemble(lay, [wins, long_])
    assert res["1"]["text"] == "K7ABC" and res["1"]["conf"] is None
    assert res["2"]["text"] == "" and "noise_phrase" in res["2"]["flags"]       # no confidence to keep it on


# ----- the benchmark's decision -----
def test_decide_prefers_large_v3_an_npu_off_the_live_device_and_provisional_only_when_fast():
    rows = [{"engine": "openvino", "model": "whisper-large-v3-int8-ov", "device": "GPU", "rtf": 0.62},
            {"engine": "openvino", "model": "whisper-large-v3-int8-ov", "device": "CPU", "rtf": 0.68},   # measured
            {"engine": "openvino", "model": "whisper-large-v3-turbo-int8-ov", "device": "GPU", "rtf": 0.22},
            {"engine": "openvino", "model": "whisper-large-v3-turbo-int8-ov", "device": "GPU", "rtf": 0.08},
            {"engine": "openvino", "model": "whisper-large-v3-int8-ov", "device": "NPU", "error": "x"}]
    d = al.decide(rows, live_device="GPU")
    assert (d["model"], d["device"], d["mode"]) == ("whisper-large-v3-int8-ov", "GPU", "final_only")
    # the CPU is NOT preferred off the live GPU (it slowed the live text more than sharing the GPU did)
    rows.append({"engine": "openvino", "model": "whisper-large-v3-int8-ov", "device": "NPU", "rtf": 0.9})
    assert al.decide(rows, live_device="GPU")["device"] == "NPU"           # an NPU within 2x: no contention
    fast = [{"engine": "faster-whisper", "model": "large-v3", "device": "cuda", "rtf": 0.05}]
    assert al.decide(fast)["mode"] == "both"
    slow = [{"engine": "openvino", "model": "whisper-large-v3-int8-ov", "device": "CPU", "rtf": 1.4},
            {"engine": "openvino", "model": "whisper-large-v3-turbo-int8-ov", "device": "CPU", "rtf": 0.4}]
    assert al.decide(slow)["model"] == "whisper-large-v3-turbo-int8-ov"
    assert al.decide([{"engine": "openvino", "model": "whisper-large-v3-turbo-int8-ov", "device": "CPU", "rtf": 2.0}]) \
        == {"mode": "none"}


# ----- the child process protocol, in process -----
def test_serve_answers_requests_and_holds_between_windows(tmp_path):
    import soundfile as sf
    p = tmp_path / "a.wav"
    sf.write(p, tones([(0.2, 0.6)], 1.0), SR, subtype="PCM_16")
    run = {"op": "run", "id": "1", "clips": [{"id": "t1", "start": 0.0, "target": True, "path": str(p)}],
           "highpass_hz": 300, "method": "long"}
    out = io.StringIO()
    al.serve({"engine": "fake", "model": "fake-large", "device": "CPU"}, io.StringIO(json.dumps(run) + "\n"), out,
             decode=dc.make({"engine": "fake", "model": "f", "device": "CPU"}))
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0] == {"ready": "fake-large@CPU (this PC)"}
    assert lines[1]["id"] == "1" and lines[1]["results"]["t1"]["text"] == "w0" and lines[1]["audio_s"] == 1.0

    # held: nothing is decoded until "go"

    class Pipe:                                     # a stdin that hands out lines as the test writes them
        def __init__(self):
            self.q = __import__("queue").Queue()

        def __iter__(self):
            while True:
                x = self.q.get()
                if x is None:
                    return
                yield x
    pipe, out2, seen = Pipe(), io.StringIO(), []
    th = threading.Thread(target=al.serve, args=({"engine": "fake", "model": "m", "device": "CPU"}, pipe, out2),
                          kwargs={"decode": lambda x, prompt="": seen.append(1) or []}, daemon=True)
    th.start()
    pipe.q.put(json.dumps({"op": "hold"}) + "\n")
    time.sleep(0.2)
    pipe.q.put(json.dumps(run) + "\n")
    time.sleep(0.3)
    assert not seen                                 # held
    pipe.q.put(json.dumps({"op": "go"}) + "\n")
    assert wait_for(lambda: seen)
    pipe.q.put(None)
    th.join(5)
    assert not th.is_alive()


# ----- AccuratePass with the home server and/or this PC -----
class FakeLocal:
    where = al.WHERE

    def __init__(self, provisional=False):
        self.provisional, self.requests, self.first_rate = provisional, [], 0.5

    def reachable(self):
        return True

    def transcribe_context(self, clips, highpass_hz, stage):
        assert all("path" in c and "wav" not in c for c in clips)        # this PC reads the WAVs itself
        self.requests.append({"stage": stage, "ids": [(c["id"], c["target"]) for c in clips]})
        return {"results": {c["id"]: {"text": f"local {stage}, this is K7ABC", "conf": None, "flags": [], "words": []}
                            for c in clips if c["target"]},
                "model": "whisper-large-v3-int8-ov@GPU (this PC)", "stage": stage, "where": al.WHERE,
                "audio_s": 2.0, "elapsed_s": 4.0}


class DownRemote(ContextRemote):
    def reachable(self):
        return False


def _station(tmp_path, texts, remote=None, local=None, **acc):
    cfg = copy.deepcopy(CFG); cfg["accurate"] = {"auto_after_min": 15, "final_after_min": 5, **acc}
    st = Station(cfg, lambda: engine.FakeEngine(texts), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    if remote:
        st._accurate_remote = remote
    st._accurate_local = local
    st.start(listen=False)
    return st


def test_no_home_server_this_pc_final_only_runs_the_final_after_the_net(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCOMM_STT_KEY", raising=False)
    loc = FakeLocal(provisional=False)
    st = _station(tmp_path, ["K7ABC checking in.", "K7ABC, you're in the log."], local=loc)
    try:
        assert not st.accurate.has_remote and st.accurate.enabled and st.accurate.available()
        assert st.snapshot()["accurate_where"] == "this PC"
        for _ in range(2):
            st.inject("ftm", np.zeros(SR * 2, np.float32))
            assert wait_for(lambda: st.tx[-1]["status"] == "final")
        time.sleep(0.3)
        assert not loc.requests and not st.accurate.status()            # not fast enough to follow: nothing yet
        st.accurate.tick(datetime.now() + timedelta(minutes=6))
        assert wait_for(lambda: all(stage_of(t) == "final" for t in st.tx[-2:]))
        a = st.tx[-1]["accurate"]
        assert a["model"] == "whisper-large-v3-int8-ov@GPU (this PC)" and a["where"] == "this PC"
        assert "conf" not in a and a["text"].startswith("local final")   # no word probabilities: no confidence
        j = next(j for j in st.accurate.status() if j["stage"] == "final")
        assert wait_for(lambda: next(x for x in st.accurate.status() if x["stage"] == "final")["state"] == "done")
        assert j["where"] == "this PC" and abs(st.accurate.rates["local"] - (0.7 * 0.5 + 0.3 * 0.5)) < 1e-6
    finally:
        st.stop.set()


def test_this_pc_final_only_rolls_the_final_pass_over_the_finished_part_of_a_long_net(tmp_path, monkeypatch):
    """Owner 2026-11-09: "run the accurate pass more often". A PC too slow to follow every transmission still gives
    the finished part of a net that is still going its final text every `rolling_min` minutes; a transmission joins
    once it ended ROLL_LAG_S ago; the net's own final pass afterwards does only what is left."""
    monkeypatch.delenv("EMCOMM_STT_KEY", raising=False)
    loc = FakeLocal(provisional=False)
    st = _station(tmp_path, ["K7ABC checking in.", "K7ABC, you're in the log.", "W9XYZ checking in."],
                  local=loc, final_after_min=30, rolling_min=5)
    try:
        for _ in range(2):
            st.inject("ftm", np.zeros(SR * 2, np.float32))
            assert wait_for(lambda: st.tx[-1]["status"] == "final")
        t0 = datetime.now()
        st.accurate.tick(t0 + timedelta(minutes=2))                      # finished, but not a few minutes' worth yet
        time.sleep(0.3)
        assert not loc.requests and not st.accurate.status()
        st.accurate.tick(t0 + timedelta(minutes=6))                      # net still going (final after 30 min)
        assert wait_for(lambda: all(stage_of(t) == "final" for t in st.tx[-2:]))
        assert [r["stage"] for r in loc.requests] == ["final"]
        j = next(j for j in st.accurate.status() if j["stage"] == "final")
        assert wait_for(lambda: st.accurate.status()[0]["state"] == "done") and j["until"] == st.tx[-1]["start"]
        st.inject("ftm", np.zeros(SR * 2, np.float32))                   # the net goes on
        assert wait_for(lambda: st.tx[-1]["status"] == "final")
        st.accurate.tick(datetime.now() + timedelta(seconds=10))         # just ended: waits for the next round
        time.sleep(0.3)
        assert len(loc.requests) == 1 and stage_of(st.tx[-1]) is None
        st.accurate.tick(datetime.now() + timedelta(minutes=40))         # the net is over: only the new one is left
        assert wait_for(lambda: stage_of(st.tx[-1]) == "final")
        last = loc.requests[-1]["ids"]
        assert [i for i, tgt in last if tgt] == [st.tx[-1]["id"]] and len(last) == 3   # the earlier two as context
    finally:
        st.stop.set()


def test_no_rolling_when_this_pc_follows_every_transmission(tmp_path, monkeypatch):
    monkeypatch.delenv("EMCOMM_STT_KEY", raising=False)
    loc = FakeLocal(provisional=True)
    st = _station(tmp_path, ["K7ABC checking in."], local=loc, final_after_min=30, rolling_min=5)
    try:
        st.inject("ftm", np.zeros(SR * 2, np.float32))
        assert wait_for(lambda: stage_of(st.tx[-1]) == "provisional")
        st.accurate.tick(datetime.now() + timedelta(minutes=6))
        time.sleep(0.3)
        assert [r["stage"] for r in loc.requests] == ["provisional"]
    finally:
        st.stop.set()


def test_the_home_server_wins_when_it_answers_and_this_pc_covers_when_it_does_not(tmp_path):
    loc, mac = FakeLocal(provisional=True), ContextRemote()
    st = _station(tmp_path, ["K7ABC checking in.", "K7ABC again."], remote=mac, local=loc)
    try:
        st.inject("ftm", np.zeros(SR * 2, np.float32))
        assert wait_for(lambda: st.tx[-1].get("accurate"))
        assert mac.requests and not loc.requests and st.tx[-1]["accurate"]["where"] == "home server"
        st.accurate.remote = DownRemote()                                 # the Mac goes away
        st.inject("ftm", np.zeros(SR * 2, np.float32))
        assert wait_for(lambda: st.tx[-1].get("accurate"))
        assert loc.requests[-1]["stage"] == "provisional" and st.tx[-1]["accurate"]["where"] == "this PC"
        assert st.accurate.where == "this PC" and st.accurate.available()
    finally:
        st.stop.set()


def test_home_server_down_and_this_pc_final_only_waits_with_a_reason(tmp_path):
    loc = FakeLocal(provisional=False)
    st = _station(tmp_path, ["K7ABC checking in."], remote=DownRemote(), local=loc)
    try:
        st.inject("ftm", np.zeros(SR * 2, np.float32))
        assert wait_for(lambda: st.accurate.status() and st.accurate.status()[0]["state"] == "unreachable")
        assert "this PC makes the final text" in st.accurate.status()[0]["error"] and not loc.requests
        assert st.accurate.available()                                    # waiting still helps: this PC will do it
    finally:
        st.stop.set()


def test_live_busy_and_the_hold_rules(tmp_path):
    st = _station(tmp_path, ["x"])
    try:
        assert not st.live_busy()
        st.radios["ftm"]["live_tx"] = "1"
        assert st.live_busy()
        st.radios["ftm"]["live_tx"] = None
        st._final_busy = True
        assert st.live_busy()
        st._final_busy = False
    finally:
        st.stop.set()
    busy = [False]
    loc = al.Local({"mode": "both", "engine": "fake", "model": "m", "device": "CPU", "rtf": 0.1},
                   busy=lambda: busy[0], power=lambda: False)
    try:
        assert loc.should_hold() == "on battery" and loc.provisional
        loc.on_battery_ok = True
        assert loc.should_hold() == ""
        busy[0] = True
        assert loc.should_hold() == "live text"
        fo = al.Local({"mode": "both", "engine": "fake", "model": "m", "device": "CPU"}, cfg={"local": "final_only"})
        assert not fo.provisional
        fo.close()
        assert al.make(NS(live_busy=lambda: False, data=tmp_path), {"local": "off"}, {"mode": "both"}) is None
        assert al.make(NS(live_busy=lambda: False, data=tmp_path), {}, {"mode": "none"}) is None
    finally:
        loc.close()


def test_local_child_process_end_to_end(tmp_path):
    """The real child (python -m station.accurate_local) with the fake recogniser: load, hold/go, answer, unload."""
    import soundfile as sf
    paths = []
    for i, spec in enumerate([[(0.2, 0.6), (1.0, 1.4)], [(0.3, 0.8)]]):
        p = tmp_path / f"{i}.wav"
        sf.write(p, tones(spec, 2.0), SR, subtype="PCM_16")
        paths.append(p)
    busy = [True]
    loc = al.Local({"mode": "both", "engine": "fake", "model": "fake-large", "device": "CPU", "rtf": 0.1},
                   busy=lambda: busy[0], cfg={"quiet_s": 0.2, "local_on_battery": True}, data_dir=tmp_path,
                   free_mb=lambda: None)        # the fake model needs no RAM (a live station on this PC holds ~10 GB)
    try:
        clips = [{"id": "a", "start": "2026-11-03T20:00:00", "target": True, "path": paths[0]},
                 {"id": "b", "start": "2026-11-03T20:00:05", "target": True, "path": paths[1]}]
        out = {}
        th = threading.Thread(target=lambda: out.update(loc.transcribe_context(clips, 0, "final")), daemon=True)
        th.start()
        time.sleep(3.0)
        assert not out                                                    # held while the live text is busy
        busy[0] = False
        th.join(30)
        assert out["results"]["a"]["text"] == "w0 w1" and out["results"]["b"]["text"] == "w2"
        assert out["model"] == "fake-large@CPU (this PC)" and out["held_s"] > 0
        p = loc.proc
        loc.unload_after = 0                                              # idle: the child is ended, RAM given back
        assert wait_for(lambda: loc.proc is None and p.poll() is not None)
    finally:
        loc.close()


def test_pick_accurate_test_block_is_varied_speech_over_one_window():
    from tools.pick_accurate import test_block
    x = test_block()
    assert 50 < len(x) / SR < 70 and x.dtype == np.float32           # two windows: the sequential walk is timed too


def test_local_refuses_to_load_when_memory_is_short(tmp_path):
    import pytest
    loc = al.Local({"mode": "final_only", "engine": "fake", "model": "m", "device": "GPU", "ram_mb": 3800},
                   free_mb=lambda: 4000, data_dir=tmp_path)
    try:
        with pytest.raises(al.LocalError, match="not enough free memory"):
            loc.transcribe_context([], 0, "final")
        assert loc.proc is None
    finally:
        loc.close()


def test_word_probabilities_are_dropped_not_waited_for_when_memory_is_short(tmp_path, capsys):
    """OpenVINO word probabilities need the model compiled twice (~1.65x): with room for the model only, the pass
    runs without them."""
    seen = {}
    loc = al.Local({"mode": "final_only", "engine": "openvino", "model": "m", "device": "GPU", "ram_mb": 4000},
                   free_mb=lambda: 6000, data_dir=tmp_path)
    loc.command = ["python", "-c", "import sys,json; print(json.dumps({'ready': 'm'})); sys.stdout.flush(); sys.stdin.read()"]
    import subprocess
    real = subprocess.Popen
    def spy(cmd, *a, **k):
        seen["spec"] = json.loads(cmd[-1]); return real(cmd, *a, **k)
    al.subprocess.Popen = spy
    try:
        try:
            loc._ensure()
        except Exception:  # noqa: BLE001 — only the spec it was started with matters here
            pass
        assert seen["spec"]["probs"] is False and "not enough for word probabilities" in capsys.readouterr().out
        loc._kill(); seen.clear()
        loc.free_mb = lambda: 20000
        try:
            loc._ensure()
        except Exception:  # noqa: BLE001
            pass
        assert seen["spec"]["probs"] is True
    finally:
        al.subprocess.Popen = real
        loc.close()


def test_the_pass_holds_when_memory_runs_short_and_is_ended_when_critical(tmp_path):
    """A laptop station's graphics driver crashed the PC with several large models on its GPU (2026-11-06)."""
    import time as _t
    mem = {"v": 8000}
    loc = al.Local({"mode": "final_only", "engine": "fake", "model": "m", "device": "GPU", "ram_mb": 100},
                   free_mb=lambda: mem["v"], data_dir=tmp_path)
    try:
        loc._free_now = 8000
        assert loc.should_hold() == ""
        loc._free_now = al.LOW_MEMORY_MB - 1
        assert loc.should_hold() == "memory short"
        killed = []
        class P:
            def poll(self): return None
        loc.proc = P(); loc._kill = lambda: (killed.append(1), setattr(loc, "proc", None))
        mem["v"] = al.CRITICAL_MEMORY_MB - 1
        for _ in range(40):
            if killed:
                break
            _t.sleep(0.1)
        assert killed
    finally:
        loc._stop.set()
