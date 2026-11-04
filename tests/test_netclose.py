"""The end of a net (station/netclose.py): hearing net control close it, who may close it, learning net control from
roll call, and the timeline after a close (tail -> pause only a radio the station resumed -> final accurate pass ->
minutes; Not closed; the operator's Pause/Resume wins; End net)."""
import copy
import json
import threading
import urllib.request
from datetime import datetime, timedelta

import pytest

from station import engine, ics
from station.core import Station
from station.netclose import accept, detect, learn_ncs
from station.server import serve

TUE = datetime(2026, 11, 3)          # a Tuesday
CFG = {"callsign": "W9XYZ",
       "radios": [{"id": "ftm", "name": "FTM-150R", "device": "NET-REC", "gate": "carrier", "freq_mhz": 146.94},
                  {"id": "ic705", "name": "IC-705", "device": "IC-705", "gate": "voice", "freq_mhz": 7.185}],
       "schedule": [{"id": "n1", "name": "Springfield County ARES net", "radio": "ftm", "freq_mhz": 146.94,
                     "days": ["Tue"], "start": "20:00", "duration_min": 60}],
       "port": 0, "bind": "127.0.0.1", "qrz_online": False}

# tonight's net as the live text had it (net control's call not in the schedule; no speaker found on the close)
REAL = [((0, 23), "Calling the Springfield County ARES net. This is a directed net.", []),
        ((1, 37), "This is N7XYZ, roll call will begin now. Please give your call and name.", ["N7XYZ"]),
        ((2, 10), "K7ABC, Pat, no traffic.", ["K7ABC"]),
        ((2, 20), "K7ABC, thank you, you're in the log.", ["K7ABC"]),
        ((9, 0), "W9XYZ, no traffic.", ["W9XYZ"]),
        ((15, 10), "you have connect control, please call now.", []),
        ((15, 25), "hearing none, we'll go ahead and close the net.", []),
        ((15, 31), "Remember, if you're in Springfield station, there's a Springfield station net at 8.", []),
        ((15, 43), "We're ready to check out our propagation.", []),
        ((15, 46), "And at 9 p.m. is the Shelbyville net.", []),
        ((15, 50), ".72. Anyway, thank you for your participation this evening. I'll turn this radio back to", []),
        ((16, 39), "Over.", []),
        ((16, 45), ".", [])]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeAccurate:
    enabled = True

    def __init__(self):
        self.calls = []

    def submit(self, rid, start, end, auto=False, force=False, stage="final"):
        self.calls.append((rid, start, end, stage))
        return {"key": f"{rid}|{start}|final", "state": "queued"}


def make(tmp_path, at, cfg=CFG, **extra):
    st = Station(copy.deepcopy(cfg) | extra, lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(at))
    st.accurate = FakeAccurate()
    return st


def say(st, rid, at, text, calls=(), who=None):
    """A transmission heard at `at` whose live text is now final (what Station.finish does, minus Whisper)."""
    st.clock.t = at
    st._tx_start(rid, at.timestamp())
    tx = st._find(st.radios[rid]["live_tx"])
    st.radios[rid]["live_tx"] = None
    tx.update(status="final", text=text, callsigns=list(calls), speaker=who, speaker_how="stated" if who else None,
              duration_s=3.0, wav=f"recordings/x/{tx['id']}.wav")
    st.clock.t = at + timedelta(seconds=5)
    st._log(dict(tx))
    st.closer.on_tx(dict(tx))
    return tx


def at(m, s=0, day=TUE):
    return day.replace(hour=20) + timedelta(minutes=m, seconds=s)


def play(st, upto=None):
    out = []
    for (m, s), text, calls in REAL:
        if upto and (m, s) > upto:
            break
        out.append(say(st, "ftm", at(m, s), text, calls))
    return out


# ---------- hearing it ----------
CLOSES = ["hearing none, we'll go ahead and close the net.", "This net is now closed.", "Closing the net.",
          "We will now secure the net.", "The net is secured. 73.", "This concludes tonight's Springfield ARES net.",
          "We return the repeater to normal amateur use.", "Returning the frequency to regular use.",
          "With that we'll close the Springfield County ARES net and return the repeater to normal use.",
          "If there are no more check-ins, we'll close the net.", "Let's close out the net.", "net closed",
          "We'll end the net here, 73 everyone.", "This is the end of the net.", "close the net now",
          "Okay, closing the nut.", "The net's closed. Thanks all."]
NOT = ["Before we close the net, any further check-ins?", "Shall we close the net?", "Is the net closed?",
       "We won't close the net yet.", "The net is closed to check-ins, go ahead with traffic.",
       'He said "this net is closed" last week.', "Net control closed the net last week.",
       "you have connect control, please call now.", "Thanks for checking in to the net.",
       "And at 9 p.m. is the Shelbyville net.", "K7ABC clear.", "Any QRT?", "Who will close the net tonight?",
       "Remember, there's a Springfield station net at 8."]
SOON = ["We'll close the net at 9.", "The net will close in 5 minutes.", "I'll be closing the net shortly.",
        "We're about to close the net."]


@pytest.mark.parametrize("text", CLOSES)
def test_closing_words(text):
    d = detect(text)
    assert d and d["kind"] == "close" and not d["weak"], text


@pytest.mark.parametrize("text", NOT)
def test_not_a_close(text):
    assert detect(text, "N7XYZ") is None, text


@pytest.mark.parametrize("text", SOON)
def test_closing_soon_is_a_hint_only(text):
    assert detect(text)["kind"] == "soon", text


def test_qrt_and_clear_only_from_net_control():
    d = detect("N7XYZ clear.", "N7XYZ")
    assert d["weak"] and accept(d, None, "N7XYZ", 30, True)[0]
    assert not accept(detect("Going QRT."), "K7ABC", "N7XYZ", 30, True)[0]       # a check-in going QRT
    assert accept(detect("Going QRT."), "N7XYZ", "N7XYZ", 30, True) == (True, "ncs")
    assert not accept(detect("Going QRT."), None, None, 30, True)[0]


def test_who_may_close_it():
    d = detect("This net is now closed.")
    assert accept(d, "N7XYZ", "N7XYZ", 1, True) == (True, "ncs")                  # net control: any time
    assert not accept(d, "K7ABC", "N7XYZ", 30, True)[0]                          # another station: never
    assert accept(d, None, "N7XYZ", 30, True) == (True, "wording")               # unknown speaker: the words
    assert not accept(d, None, "N7XYZ", 4, True)[0]                              # ... after 5 min of net
    assert not accept(d, None, None, 30, False)[0]                               # ... inside the window
    assert not accept(detect("We'll close the net at 9."), "N7XYZ", "N7XYZ", 30, True)[0]


def test_net_control_learned_from_roll_call():
    v = lambda m, text, calls, **k: {"start": at(m).isoformat(), "text": text, "callsigns": calls, **k}
    assert learn_ncs([v(0, "Calling the ARES net.", []), v(1, "This is N7XYZ, roll call will begin now.", ["N7XYZ"]),
                      v(2, "K7ABC checking in.", ["K7ABC"])]) == "N7XYZ"
    assert learn_ncs([v(0, "K7ABC checking in, this is K7ABC.", ["K7ABC"])]) is None      # a check-in is not ncs
    assert learn_ncs([v(0, "This is K7ABC, how copy?", ["K7ABC"])]) is None              # no net-control words
    assert learn_ncs([v(0, "Good evening, any check-ins for the net", [], speaker="W9XYZ",
                        speaker_how="stated")]) == "W9XYZ"
    assert learn_ncs([v(25, "x", []), v(30, "This is N7XYZ, net control.", ["N7XYZ"])]) == "N7XYZ"
    assert learn_ncs([v(0, "x", []), v(30, "This is N7XYZ, net control.", ["N7XYZ"])]) is None     # too late


# ---------- the timeline ----------
def test_real_net_close_tail_pause_final_pass_and_undo(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12))
    st.set_paused("ftm", True); st.nets.tick()                 # paused by hand: the schedule resumes it for the net
    st.nets.tick(at(-1))
    assert not st.radios["ftm"]["paused"]
    play(st, upto=(15, 10))
    key = st.tx[-1]["net"]["key"]
    assert st.closer.learned[key] == "N7XYZ"                   # roll call: "This is N7XYZ, roll call will begin now"
    assert key not in st.closer.closes
    close_tx = say(st, "ftm", at(15, 25), "hearing none, we'll go ahead and close the net.")
    c = st.closer.closes[key]
    assert c["state"] == "tail" and c["at"] == at(15, 25).isoformat() and c["by"] == "N7XYZ" and c["how"] == "wording"
    inst = st.history.instance(c["inst"])
    assert inst["closed_at"] == close_tx["start"] and inst["closed_by"] == "N7XYZ" and inst["ncs"] == "N7XYZ"
    for (m, s), text, calls in REAL[7:]:                       # the 73s and announcements: still the net
        t = say(st, "ftm", at(m, s), text, calls)
        assert t["net"]["key"] == key and "after" not in t
        st.closer.tick()
    assert not st.radios["ftm"]["paused"] and st.closer.closes[key]["state"] == "tail"
    assert not st.accurate.calls                                # the final pass waits for the tail
    st.closer.tick(at(17, 30))                                  # tail over (2 min after the close was heard)
    assert st.radios["ftm"]["paused"] and st.radios["ftm"]["paused_by"] == "schedule"
    assert st.radios["ftm"]["closed"]["name"] == "Springfield County ARES net"
    assert st.accurate.calls == [("ftm", at(0, 23).isoformat(), at(16, 45).isoformat(), "final")]   # whole net
    assert st.closer.closes[key]["state"] == "closed" and st.closer.closes[key]["paused"]
    # the schedule does not resume a closed net's radio (a restart, or the window ticking on)
    st.nets._want.clear()
    st.nets.tick(at(30))
    assert st.radios["ftm"]["paused"]
    again = make(tmp_path, at(31))
    again.nets.tick()
    assert again.radios["ftm"]["paused"] and again.closer.closes[key]["state"] == "closed"
    # someone talks after the close (the operator resumed by hand): a new conversation, not the net
    st.set_paused("ftm", False)
    late = say(st, "ftm", at(25), "K7ABC, W9XYZ, are you around?", ["K7ABC", "W9XYZ"])
    assert late["after"] == key and "net" not in late
    ss = ics.sessions(list(st.tx), "ftm")
    assert len(ss) == 2 and ss[0]["end"] == at(16, 45).isoformat() and ss[1]["start"] == late["start"]
    assert st.history.instance_of("ftm", late["start"]) != c["inst"]
    # Not closed: back into the net, and the net runs to its scheduled end
    st.set_paused("ftm", True, by="schedule"); st.closer.closes[key]["paused"] = True   # as if still paused by it
    st.closer.undo("ftm", key)
    assert not st.radios["ftm"]["paused"] and "closed" not in st.radios["ftm"]
    assert st._find(late["id"])["net"]["key"] == key and "after" not in st._find(late["id"])
    assert st.history.instance(c["inst"])["closed_at"] is None
    assert st.history.instance_of("ftm", late["start"]) == c["inst"]
    assert len(ics.sessions(list(st.tx), "ftm")) == 1
    # the undo survives a restart's reading of the day log
    again = make(tmp_path, at(40))
    again.load_today()
    assert again._find(late["id"])["net"]["key"] == key and "after" not in again._find(late["id"])
    # a later, real close is heard again
    say(st, "ftm", at(40), "This net is now closed, 73.")
    assert st.closer.closes[key]["state"] == "tail"


def test_a_radio_listening_before_the_net_is_never_paused(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12))
    st.nets.tick(at(-1))                                       # listening all along: nothing to resume
    play(st, upto=(15, 25))
    st.closer.tick(at(18))
    key = st.tx[-1]["net"]["key"]
    assert st.closer.closes[key]["state"] == "closed" and not st.radios["ftm"].get("paused")
    assert st.accurate.calls and st.accurate.calls[0][3] == "final"        # the final pass runs early anyway


def test_the_operators_pause_or_resume_during_the_tail_wins(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12))
    st.set_paused("ftm", True); st.nets.tick()
    st.nets.tick(at(-1))
    play(st, upto=(15, 25))
    st.set_paused("ftm", True); st.set_paused("ftm", False)    # the operator pressed Pause, then Resume
    st.closer.tick(at(18))
    assert not st.radios["ftm"]["paused"]
    st.nets.tick(at(65))                                       # and the net's end leaves it as the operator chose
    assert not st.radios["ftm"]["paused"]


def test_listen_only_during_nets_pauses_at_the_close(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12))
    st.nets.set_listen("ftm", {"scheduled": True})
    assert st.radios["ftm"]["paused"]
    st.nets.tick(at(-1))
    assert not st.radios["ftm"]["paused"]
    play(st, upto=(15, 25))
    st.closer.tick(at(18))
    assert st.radios["ftm"]["paused"] and st.radios["ftm"]["paused_by"] == "schedule"
    st.nets.set_listen("ftm", {"scheduled": True})             # the window "opens" again: stays paused
    assert st.radios["ftm"]["paused"]
    st.nets.tick(TUE.replace(day=10, hour=19, minute=59))      # next week's net resumes it
    assert not st.radios["ftm"]["paused"]


def test_unscheduled_conversation_closes_but_never_pauses(tmp_path):
    st = make(tmp_path, TUE.replace(hour=10))
    t0 = TUE.replace(hour=10)
    say(st, "ic705", t0, "This is K7ABC, calling the Springfield HF net, any check-ins?", ["K7ABC"])
    say(st, "ic705", t0 + timedelta(minutes=2), "W9XYZ, no traffic.", ["W9XYZ"])
    say(st, "ic705", t0 + timedelta(minutes=3), "This net is now closed.")            # 3 min: too early by words
    assert not st.closer.closes
    say(st, "ic705", t0 + timedelta(minutes=8), "That concludes the net, 73.")
    key = f"ic705@{t0.isoformat()}"
    assert st.closer.closes[key]["state"] == "tail" and not st.closer.closes[key]["scheduled"]
    assert st.closer.learned[key] == "K7ABC"
    st.closer.tick(t0 + timedelta(minutes=11))
    assert not st.radios["ic705"].get("paused") and st.accurate.calls[0][:2] == ("ic705", t0.isoformat())
    late = say(st, "ic705", t0 + timedelta(minutes=12), "K7ABC, W9XYZ, thanks for the net.", ["K7ABC", "W9XYZ"])
    assert late["after"] == key and len(ics.sessions(list(st.tx), "ic705")) == 2


def test_another_station_saying_it_or_a_close_soon_is_not_a_close(tmp_path):
    cfg = copy.deepcopy(CFG); cfg["schedule"][0]["ncs"] = "K7NET"
    st = make(tmp_path, TUE.replace(hour=12), cfg)
    say(st, "ftm", at(0), "This is K7NET, net control, calling the net.", ["K7NET"], who="K7NET")
    say(st, "ftm", at(10), "This is K7ABC, I think we'll close the net now.", ["K7ABC"], who="K7ABC")
    assert not st.closer.closes
    say(st, "ftm", at(20), "We'll close the net in about 5 minutes.")
    assert not st.closer.closes and st.closer.soon["ftm"]["phrase"] == "close the net"
    say(st, "ftm", at(25), "K7NET clear.", ["K7NET"])
    assert next(iter(st.closer.closes.values()))["how"] == "ncs"


def test_end_net_by_the_operator(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12))
    st.set_paused("ftm", True); st.nets.tick()
    st.nets.tick(at(-1))
    play(st, upto=(9, 0))
    st.clock.t = at(12)
    st.closer.end_now("ftm")
    key = st.tx[-1]["net"]["key"]
    c = st.closer.closes[key]
    assert c["state"] == "closed" and c["manual"] and c["by"] == "operator"           # no tail
    assert st.radios["ftm"]["paused"] and st.radios["ftm"]["paused_by"] == "operator"
    assert st.accurate.calls == [("ftm", at(0, 23).isoformat(), at(9).isoformat(), "final")]
    assert st.history.instance(c["inst"])["closed_by"] == "operator"
    st.nets._want.clear(); st.nets.tick(at(20))                 # the schedule leaves it ended
    assert st.radios["ftm"]["paused"]
    st.closer.undo("ftm", key)                                  # Not closed: listening again, as the schedule had it
    assert not st.radios["ftm"]["paused"]
    st.nets.tick(at(65))                                        # ... and paused after the net, as before it
    assert st.radios["ftm"]["paused"]
    st.nets.tick(TUE.replace(day=10, hour=19, minute=59))       # the next net still resumes it
    assert not st.radios["ftm"]["paused"]


def test_end_net_ends_a_tail_now_and_the_api(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12))
    play(st, upto=(15, 25))
    key = st.tx[-1]["net"]["key"]
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda p, b: json.loads(urllib.request.urlopen(urllib.request.Request(
        base + p, data=json.dumps(b).encode(), method="POST", headers={"Content-Type": "application/json"})).read())
    try:
        d = post("/api/netclose/ftm/end", {"key": key})
        c = next(x for x in d["closes"] if x["key"] == key)
        assert c["state"] == "closed" and st.radios["ftm"]["paused"] and st.accurate.calls
        d = post("/api/netclose/ftm/undo", {"key": key})
        assert next(x for x in d["closes"] if x["key"] == key)["state"] == "open" and not st.radios["ftm"]["paused"]
        post("/api/netclose/ftm/save_ncs", {"key": key})        # "Save N7XYZ as net control"
        assert st.cfg["schedule"][0]["ncs"] == "N7XYZ"
        with pytest.raises(urllib.error.HTTPError):
            post("/api/netclose/nope/undo", {"key": key})
    finally:
        srv.shutdown()


def test_close_detection_can_be_turned_off(tmp_path):
    st = make(tmp_path, TUE.replace(hour=12), close_detect=False)
    play(st)
    assert not st.closer.closes
