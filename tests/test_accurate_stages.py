"""The two-stage accurate pass in the station (station/accurate.py + Station.apply_accurate): a provisional text with
the net before it as each transmission ends, a final text for the whole net when it goes quiet; the live text is
never replaced; results persist; an older Mac service (no /accurate/context) falls back to one at a time."""
import copy
from datetime import datetime, timedelta

import numpy as np

from station import engine
from station.accurate import NoContextEndpoint, stage_of
from station.core import Station
from tests.test_station import CFG, FakeBook, wait_for


class ContextRemote:
    """POST /accurate/context, faked: returns '<stage> text for <id>' per target, or '' for ids in `silent`."""
    def __init__(self, silent=()):
        self.requests, self.silent, self.single = [], set(silent), 0

    def reachable(self):
        return True

    def transcribe(self, wav, prompt, highpass_hz):
        self.single += 1
        return {"text": "one at a time K7ABC", "model": "old", "audio_s": 1.0}

    def transcribe_context(self, clips, highpass_hz, stage):
        assert highpass_hz == 300 and all(c["wav"][:4] == b"RIFF" for c in clips)
        self.requests.append({"stage": stage, "ids": [(c["id"], c["target"]) for c in clips]})
        res = {c["id"]: ({"text": "", "conf": 0.0, "flags": ["empty", "noise_phrase"], "words": []} if c["id"] in self.silent
                         else {"text": f"{stage} text, this is K7ABC", "conf": 0.8, "flags": [], "words": []})
               for c in clips if c["target"]}
        return {"results": res, "model": "fake-large", "stage": stage, "audio_s": 3.0, "elapsed_s": 0.1}


def _station(tmp_path, remote, texts, **acc):
    cfg = copy.deepcopy(CFG); cfg["accurate"] = {"auto_after_min": 15, "final_after_min": 5, **acc}
    st = Station(cfg, lambda: engine.FakeEngine(texts), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
    st._accurate_remote = remote
    st.start(listen=False)
    return st


def test_provisional_then_final_and_persisted(tmp_path):
    r = ContextRemote()
    st = _station(tmp_path, r, ["K7ABC checking in.", "K7ABC, you're in the log.", "Thank you."])
    try:
        for _ in range(3):
            st.inject("ftm", np.zeros(16000 * 2, np.float32))
            assert wait_for(lambda: st.tx[-1]["status"] == "final" and st.tx[-1].get("accurate"))
        first, last = st.tx[-3], st.tx[-1]
        assert first["accurate"]["stage"] == "provisional" and first["accurate"]["text"].startswith("provisional text")
        assert first["text"] == "K7ABC checking in." and first["accurate"]["conf"] == 0.8      # live text kept beside
        # each provisional request carries the net before it as context (not as a target)
        assert r.requests[-1]["stage"] == "provisional" and r.requests[-1]["ids"][-1] == (last["id"], True)
        assert any(not tgt for _, tgt in r.requests[-1]["ids"])
        assert st.winlink_preview("ftm")["accurate_missing"] == 3                              # provisional = still waiting
        st.accurate.tick(datetime.now() + timedelta(minutes=1))                                # the net is not over yet
        assert not any(j["stage"] == "final" for j in st.accurate.status())
        st.accurate.tick(datetime.now() + timedelta(minutes=6))                                # 5 quiet minutes: final
        assert wait_for(lambda: all(stage_of(t) == "final" for t in st.tx[-3:]))
        fin = [q for q in r.requests if q["stage"] == "final"]
        assert len(fin) == 1 and [tgt for _, tgt in fin[0]["ids"]] == [True, True, True]       # one block, whole net
        assert st.tx[-1]["accurate"]["text"].startswith("final text") and st.tx[-1]["text"] == "Thank you."
        j = next(j for j in st.accurate.status() if j["stage"] == "final")
        assert wait_for(lambda: next(j for j in st.accurate.status() if j["stage"] == "final")["state"] == "done")
        assert st.winlink_preview("ftm")["accurate_missing"] == 0
        # a provisional result that arrives late never undoes the final one
        st.apply_accurate(first["id"], {"text": "late provisional", "stage": "provisional", "model": "x"})
        assert st._find(first["id"])["accurate"]["stage"] == "final"
        again = Station(copy.deepcopy(st.cfg), lambda: engine.FakeEngine(), callbook=FakeBook({"K7ABC": "Pat"}), data_dir=tmp_path)
        again.load_today()
        assert [stage_of(t) for t in again.tx] == ["final"] * 3                                # survives a restart
        assert again.tx[-1]["accurate"]["callsigns"] == ["K7ABC"]                              # normalize ran on it
    finally:
        st.stop.set()


def test_final_sets_a_noise_clip_aside_and_keeps_the_live_text(tmp_path):
    r = ContextRemote()
    st = _station(tmp_path, r, ["K7ABC checking in.", "So."])
    try:
        for _ in range(2):
            st.inject("ftm", np.zeros(16000 * 2, np.float32))
            assert wait_for(lambda: st.tx[-1]["status"] == "final" and st.tx[-1].get("accurate"))
        r.silent.add(st.tx[-1]["id"])                       # the final pass hears nothing in the 2nd clip
        st.accurate.tick(datetime.now() + timedelta(minutes=6))
        assert wait_for(lambda: st.tx[-1].get("accurate_echo"))
        t = st.tx[-1]
        assert "accurate" not in t and t["accurate_echo"]["stage"] == "final" and t["text"] == "So."
        assert "noise_phrase" in t["accurate_echo"]["flags"] and stage_of(t) == "final"
        # a low-confidence text (mostly a neighbouring turn's words on the real nets) is kept aside too
        st.apply_accurate(t["id"], {"text": "Now and then I get jittery", "conf": 0.23, "flags": ["low_conf"],
                                    "stage": "final", "model": "x"})
        t = st._find(t["id"])
        assert "accurate" not in t and t["accurate_echo"]["text"] == "Now and then I get jittery"
    finally:
        st.stop.set()


def test_an_older_mac_falls_back_to_one_at_a_time(tmp_path):
    class Old(ContextRemote):
        def transcribe_context(self, clips, highpass_hz, stage):
            raise NoContextEndpoint()
    r = Old()
    st = _station(tmp_path, r, ["K7ABC checking in."])
    try:
        st.inject("ftm", np.zeros(16000 * 2, np.float32))
        assert wait_for(lambda: st.tx[-1].get("accurate"))
        assert r.single == 1 and not st.accurate.context and stage_of(st.tx[-1]) == "final"
        assert st.tx[-1]["accurate"]["text"] == "one at a time K7ABC"
    finally:
        st.stop.set()


def test_requests_block_a_long_net_with_context_on_both_sides(tmp_path):
    from station import accurate as acc
    st = _station(tmp_path, ContextRemote(), ["x"], live=False)
    try:
        t0 = datetime(2026, 11, 2, 20, 0, 0)
        st.tx[:] = [{"id": str(i), "radio": "ftm", "status": "final", "freq_mhz": 146.94, "wav": f"r/{i}.wav",
                     "start": (t0 + timedelta(seconds=12 * i)).isoformat(), "duration_s": 10.0, "text": ""}
                    for i in range(60)]                                   # 10 min of 10 s transmissions
        j = {"radio": "ftm", "start": st.tx[0]["start"], "end": st.tx[-1]["start"]}
        reqs = st.accurate._requests(j, st.tx, "final")
        assert [len(tg) for _, tg in reqs] == [24, 24, 12]               # 240 s of targets per request
        clips, targets = reqs[1]
        ctx = [c["tx"]["id"] for c in clips if not c["target"]]
        assert ctx == ["21", "22", "23", "48", "49", "50"]               # 30 s each side
        # provisional: the target with the ~25 s before it
        p = st.accurate._requests(j, [st.tx[30]], "provisional")
        assert [(c["tx"]["id"], c["target"]) for c in p[0][0]] == [("29", False), ("30", True)]
        assert acc.PROV_CONTEXT_S - 10 < 20
    finally:
        st.stop.set()
