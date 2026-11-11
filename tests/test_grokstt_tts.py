"""Grok speech-to-text as a compared engine (never queued) and Sidekick's voice (for the operator's ears only)."""
import copy
import json
import os
import urllib.error

import pytest

from station import engine, grokstt, tts
from station.core import Station
from tests.test_cat import CFG, Clock, TUE as T


@pytest.fixture
def st(tmp_path):
    os.environ["XAI_API_KEY"] = "xai-" + "b" * 30
    s = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path,
                clock=Clock(T.replace(hour=20, minute=5)))
    yield s
    del os.environ["XAI_API_KEY"]


def _tx(st, tid="t1", dur=4.0, text="W9XYZ this is K7ABC checking in", **kw):
    wav = st.data / "recordings" / f"{tid}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF....WAVEfmt fake")
    t = {"id": tid, "radio": "ic", "status": "final", "start": T.replace(hour=20, minute=4).isoformat(),
         "duration_s": dur, "text": text, "callsigns": ["K7ABC"], "took_s": 0.4,
         "wav": str(wav.relative_to(st.data)), **kw}
    st.tx.append(t)
    return t


def test_multipart_puts_the_file_last():
    body, ctype = grokstt.multipart([("model", "m"), ("keyterm", "SARC")], b"WAVDATA")
    assert ctype.startswith("multipart/form-data; boundary=")
    assert body.index(b'name="model"') < body.index(b'name="keyterm"') < body.index(b'name="file"')
    assert body.rstrip().endswith(b"--")


def test_keyterms_are_the_call_the_local_names_and_the_net():
    e = [{"name": "SARC", "meaning": "Springfield Amateur Radio Club", "web": "sarconline.org"}]
    k = grokstt.keyterms_for("W9XYZ", e, "Springfield Net")
    assert k == ["W9XYZ", "SARC", "Springfield Amateur Radio Club", "sarconline.org", "Springfield Net"]
    assert all(len(x) <= 50 for x in grokstt.keyterms_for("W9XYZ", [{"name": "x" * 80}]))


def test_off_until_a_radio_is_chosen_then_live_and_compared(st):
    sent = []

    def fake(key, wav, language, terms):
        sent.append((language, list(terms)))
        return {"text": "W9XYZ this is K7ABC checking in", "language": "en", "duration": 4.0,
                "words": [{"text": "W9XYZ", "start": 0.1, "end": 0.6, "confidence": 0.9}]}
    g = st.grokstt
    g.call = fake
    st.roster = set(st.roster) | {"K7ABC"}                       # written calls count when known (as live)
    t = _tx(st)
    assert g.on_final(t) is None and not sent                   # off by default
    with pytest.raises(ValueError):
        g.settings({"radios": {"nope": True}})
    g.settings({"radios": {"ic": True}})
    g.on_final(t).join(5)
    assert sent and sent[0][0] == "en" and "W9XYZ" in sent[0][1]
    rows = g.results("20261103")
    assert rows[0]["text"].startswith("W9XYZ") and rows[0]["took_s"] >= 0 and rows[0]["words"][0]["end"] == 0.6

    t["accurate"] = {"text": "W9XYZ this is K7ABC checking in", "callsigns": ["K7ABC"]}
    t["text"] = "W9XYZ this is W9QXJ checking"                   # the live text was worse
    c = g.compare("20261103", rows=st.tx)
    s = c["summary"]["accurate"]
    assert s["n"] == 1 and s["grok_wer"] == 0.0 and s["live_wer"] > 0
    assert s["grok_call_recall"] == 1.0 and c["items"][0]["ref_from"] == "accurate"


def test_never_queued_offline_busy_long_noise_or_over_the_minutes(st):
    calls = []

    def down(key, wav, language, terms):
        calls.append(1)
        raise urllib.error.URLError("no route to host")
    g = st.grokstt
    g.call = down
    g.settings({"radios": {"ic": True}, "minutes_per_day": 1})
    g.on_final(_tx(st, "a")).join(5)
    assert g.view()["online"]["online"] is False and calls == [1]
    assert g.on_final(_tx(st, "b")) is None and calls == [1]    # offline: skipped, not kept for later
    assert g.view()["today"]["skipped"]["offline"] == 1
    g.online.ok()
    assert g.on_final(_tx(st, "c", dur=200)) is None             # an open squelch
    assert g.on_final(_tx(st, "d", noise=True)) is None
    g.inflight = 2
    assert g.on_final(_tx(st, "e")) is None                      # two out already: skipped
    g.inflight = 0
    assert g.on_final(_tx(st, "f", dur=59)) is None              # 4 s already used of a 60 s day
    sk = g.view()["today"]["skipped"]
    assert sk == {"offline": 1, "longer than 2 minutes": 1, "noise": 1, "busy": 1, "over the day's minutes": 1}
    with pytest.raises(ValueError):                              # a chosen past net: refused offline, not queued
        g.online.failed("down")
        g.run_past(["a"])


def test_a_correction_is_the_human_reference(st):
    g = st.grokstt
    g.call = lambda *a: {"text": "K7ABC good evening", "language": "en", "duration": 2}
    g.settings({"radios": {"ic": True}})
    t = _tx(st)
    g.on_final(t).join(5)
    t["corrected"] = {"text": "K7ABC good evening", "callsigns": ["K7ABC"]}
    c = g.compare("20261103", rows=st.tx)
    assert "corrected" in c["summary"] and c["summary"]["corrected"]["grok_wer"] == 0.0


def test_the_voice_is_chosen_in_settings_and_clips_are_paid_for_once(st):
    made = []

    def fake(key, text, voice, speed, replace=None):
        made.append((voice, speed, replace))
        return b"ID3mp3"
    st.tts.synth_fn = fake
    st.tts.voices_fn = lambda key: [{"voice_id": "eve", "name": "Eve", "language": "en"},
                                    {"voice_id": "rex", "name": "Rex", "language": "en"}]
    v = st.tts.view()
    assert [x["voice_id"] for x in v["voices"]] == ["eve", "rex"] and v["voice"] == ""
    assert st.tts.audition("rex") == b"ID3mp3" and st.tts.audition("rex") == b"ID3mp3"
    assert len(made) == 1 and made[0][2] == {"W9XYZ": "W 9 X Y Z"}       # the call said letter by letter
    assert st.tts.view()["auditioned"] == {"eve": False, "rex": True}
    st.agent.settings({"voice": "rex", "speed": 1.1})
    assert st.tts.view()["voice"] == "rex"
    with pytest.raises(ValueError):
        st.agent.settings({"speed": 3})
    st.agent.drafts.append({"id": "d1", "say": "W9XYZ, checking in."})
    st.tts.draft("d1")
    assert made[-1][:2] == ("rex", 1.1)


def test_offline_only_clips_already_made_play(st):
    st.tts.synth_fn = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down"))
    st.tts.voices_fn = lambda key: (_ for _ in ()).throw(urllib.error.URLError("down"))
    assert len(st.tts.view()["voices"]) == len(tts.VOICES)               # the built-in list
    with pytest.raises(LookupError):
        st.tts.audition("eve")
    with pytest.raises(LookupError):                                     # inside the back-off: not even tried
        st.tts.audition("ara")


def test_a_call_grok_spells_letter_by_letter_is_joined():
    assert grokstt.join_spelled("Welcome, K I 5 Q X N. K I five Q X N.") == "Welcome, KI5QXN. KI5QXN."
    assert grokstt.join_spelled("Good evening N seven Q X J, over") == "Good evening N7QXJ, over"
    assert grokstt.join_spelled("I am a big fan of A B C D") == "I am a big fan of A B C D"   # no call shape
