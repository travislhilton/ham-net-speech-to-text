"""Segmenting rules on synthetic audio: pre-roll, cuts in the middle of a pause, fragments kept with their
neighbour, a voiceless repeater tail, carrier dips, weak stations under a loud squelch hiss, the HF merge wait."""
import numpy as np

from station.segmenter import Segmenter, VoicePauses

SR, N = 16000, 512
rng = np.random.default_rng(7)


def speech(s, amp=0.3):
    """1 kHz tone in syllable-like bursts (3 Hz) over a faint floor: voice-band energy that comes and goes."""
    t = np.arange(int(SR * s)) / SR
    return amp * np.sin(2 * np.pi * 1000 * t) * (np.sin(2 * np.pi * 3 * t) > -0.3) + 0.003 * rng.standard_normal(len(t))


def quiet(s):
    return 0.003 * rng.standard_normal(int(SR * s))


def hiss(s):
    return 0.5 * rng.standard_normal(int(SR * s))       # an FM data jack with no carrier: loud, broadband


class Always:
    def is_speech(self, f):
        return True


class Script:
    """Gate state per frame from a list (then closed)."""
    def __init__(self, pattern):
        self.p = list(pattern)

    def is_speech(self, _f):
        return self.p.pop(0) if self.p else False


class Loud:
    """A carrier gate stand-in: open when the frame is not hiss (hiss is far louder than anything else here)."""
    def is_speech(self, f):
        return float(np.sqrt(np.mean(f ** 2))) < 0.35


def feed(seg, audio):
    audio = np.concatenate([audio, np.zeros((-len(audio)) % N)]).astype(np.float32)
    return [ev for i in range(0, len(audio), N) for ev in seg.feed(audio[i:i + N])]


def ends(evs, keep_kerchunks=False):
    return [e for e in evs if e[0] == "end" and (keep_kerchunks or not e[2]["kerchunk"])]


def first_loud(audio, th=0.1):
    return int(np.argmax(np.abs(audio) > th)) / SR


def last_loud(audio, th=0.1):
    return (len(audio) - int(np.argmax(np.abs(audio[::-1]) > th))) / SR


def test_preroll_keeps_the_syllable_a_late_gate_missed():
    """The VAD opens 0.4 s after the voice began: 600 ms of pre-roll gives the first syllable back."""
    audio = np.concatenate([quiet(2.0), speech(2.0), quiet(2.0)])
    onset = int(2.0 * SR / N)
    for pre_ms, kept in ((250, False), (600, True)):
        gate = Script([False] * (onset + 12) + [True] * 50)          # opens 12 frames (0.38 s) late
        seg = Segmenter(gate, SR, 32, preroll_ms=pre_ms, hangover_ms=320, clock=lambda: 0)
        (e,) = ends(feed(seg, audio))
        lead = first_loud(e[1])                                       # where the tone starts in the piece
        assert (lead > 0.1) is kept                                   # 600 ms: the onset is inside, after a lead-in


def test_split_cuts_in_the_middle_of_the_pause():
    """Neither piece starts or ends on a word: half of a 1.2 s pause stays on each side."""
    audio = np.concatenate([quiet(6), speech(4), quiet(1.2), speech(3), quiet(1.2), speech(3), quiet(0.3)])
    seg = Segmenter(Always(), SR, 32, split_pause_ms=800, clock=lambda: 0)
    pieces = ends(feed(seg, audio))
    assert len(pieces) == 2 and all(p[2]["split"] for p in pieces)  # the third is still open (carrier up)
    a, b = pieces[0][1], pieces[1][1]
    assert len(a) / SR - last_loud(a) > 0.4 and first_loud(b) > 0.4  # >= ~0.5 s of the pause on each side
    assert abs(pieces[1][2]["t0"] - len(a) / SR) < 0.05               # the next piece starts where this one ended


def test_a_fragment_between_two_pauses_stays_with_its_neighbour():
    """'Over.' (0.2 s) alone between two pauses is not a transmission of its own, nor dropped."""
    over = speech(0.25)
    audio = np.concatenate([quiet(6), speech(3), quiet(1.2), over, quiet(1.2), speech(3), quiet(1.2), speech(2)])
    seg = Segmenter(Always(), SR, 32, split_pause_ms=800, clock=lambda: 0)
    evs = feed(seg, audio)
    assert not [e for e in ends(evs, True) if e[2]["kerchunk"]]
    pieces = ends(evs)
    assert len(pieces) == 2
    first = pieces[0][1]
    # the first piece runs through the fragment: 6 + 3 + 1.2 + 0.25 s, then half the second pause
    assert 10.4 < len(first) / SR < 11.4 and last_loud(first) > 10.3


def test_dead_carrier_before_speech_is_dropped_and_the_tail_stays():
    """A carrier with nobody talking for seconds, then speech: the dead part goes as a kerchunk and the speech is
    its own piece; the repeater tail after the last word stays with it (no blank piece)."""
    first = np.concatenate([quiet(6), speech(3), quiet(1)])          # teaches the levels; closes on the hiss
    second = np.concatenate([quiet(4), speech(3), quiet(1.5)])
    audio = np.concatenate([first, hiss(2), second, hiss(2)])
    seg = Segmenter(Loud(), SR, 32, split_pause_ms=800, hangover_ms=900, clock=lambda: 0)
    allends = ends(feed(seg, audio), True)
    assert [(e[2]["kerchunk"], bool(e[2].get("split"))) for e in allends] == [(False, False), (True, True), (False, False)]
    dead, last = allends[1][1], allends[2][1]
    assert 2.0 < len(dead) / SR < 3.0                                # the dead carrier, to the middle of the pause
    assert first_loud(last) > 1.5 and len(last) / SR > first_loud(last) + 3 + 1.4   # lead-in, speech, tail


def test_carrier_dip_is_a_turn_boundary():
    """A 0.13 s carrier dip (squelch-tail burst) with a short pause around it splits the carrier."""
    pre = quiet(6)
    a, b = speech(3), speech(3)
    dip = hiss(0.13)
    audio = np.concatenate([pre, a, quiet(0.15), dip, quiet(0.15), b, quiet(0.5), hiss(1.5)])
    seg = Segmenter(Loud(), SR, 32, split_pause_ms=800, dip_split_ms=96, hangover_ms=900, clock=lambda: 0)
    pieces = ends(feed(seg, audio))
    assert len(pieces) == 2 and pieces[0][2]["split"]
    seg = Segmenter(Loud(), SR, 32, split_pause_ms=800, dip_split_ms=0, hangover_ms=900, clock=lambda: 0)
    assert len(ends(feed(seg, audio))) == 1                     # dip splits off: the 0.4 s pause is too short


def test_weak_station_is_not_a_pause_under_loud_squelch_hiss():
    """The levels are learned only while the gate is open: the hiss between overs is far louder than a weak
    station, and with it in the history the weak station's whole check-in read as a pause."""
    vp = VoicePauses(SR, N, frame_ms=32)
    frames = lambda x: [x[i:i + N] for i in range(0, len(x) - N + 1, N)]
    for f in frames(hiss(20)):
        vp.quiet(f, gate_open=False)
    for f in frames(np.concatenate([quiet(3), speech(3, amp=0.05), quiet(2)])):
        vp.quiet(f, gate_open=True)
    weak = [vp.quiet(f) for f in frames(speech(1.0, amp=0.05))]
    assert sum(1 for q in weak if q is False) > 0.5 * len(weak)
    # end to end: hiss, then a weak station on a hanging carrier, nothing dropped
    audio = np.concatenate([hiss(10), quiet(1), speech(2, amp=0.05), quiet(1.2), speech(2, amp=0.05), quiet(1.0),
                            hiss(2)])
    seg = Segmenter(Loud(), SR, 32, split_pause_ms=800, hangover_ms=900, clock=lambda: 0)
    allends = ends(feed(seg, audio), True)
    assert allends and not any(e[2]["kerchunk"] for e in allends)


def test_merge_wait_joins_a_short_piece_with_its_continuation():
    """HF voice gate: 'K-' (0.3 s, a kerchunk alone), the VAD closes for 1.2 s, then '7ABC' (2 s): one
    transmission when the gap fits in hangover + merge_gap; two (the first dropped) without the merge wait."""
    pattern = [True] * 9 + [False] * 37 + [True] * 62 + [False] * 60
    audio = np.zeros(len(pattern) * N, np.float32)
    seg = Segmenter(Script(pattern), SR, 32, hangover_ms=900, merge_gap_ms=600, preroll_ms=96, clock=lambda: 0)
    evs = ends(feed(seg, audio), True)
    assert len(evs) == 1 and not evs[0][2]["kerchunk"] and evs[0][2]["bridged"] == 1
    assert [e[0] for e in feed(Segmenter(Script(pattern), SR, 32, merge_gap_ms=600), audio)].count("start") == 1
    seg = Segmenter(Script(pattern), SR, 32, hangover_ms=900, merge_gap_ms=0, preroll_ms=96, clock=lambda: 0)
    assert [e[2]["kerchunk"] for e in ends(feed(seg, audio), True)] == [True, False]


def test_merge_wait_that_finds_nothing_is_trimmed():
    """A short piece with no continuation ends as before: pre-roll + speech + hangover, no merge-wait audio."""
    pattern = [False] * 5 + [True] * 40 + [False] * 80
    seg = Segmenter(Script(pattern), SR, 32, hangover_ms=320, merge_gap_ms=600, preroll_ms=96, clock=lambda: 0)
    (e,) = ends(feed(seg, np.zeros(len(pattern) * N, np.float32)))
    assert len(e[1]) == (3 + 40 + 10) * N and "bridged" not in e[2]
    long = [True] * 120 + [False] * 40                              # longer than merge_max_s: no wait at all
    seg = Segmenter(Script(long), SR, 32, hangover_ms=320, merge_gap_ms=600, merge_max_s=3.0, clock=lambda: 0)
    evs = feed(seg, np.zeros(len(long) * N, np.float32))
    assert [i for i, e in enumerate(evs) if e[0] == "end"] and len(ends(evs)[0][1]) == (120 + 10) * N


def test_station_segmenter_defaults_per_gate(tmp_path):
    from station.core import Station
    st = Station({"radios": [{"id": "fm", "name": "FM", "device": "x", "gate": "carrier"}]}, lambda: None,
                 data_dir=tmp_path)
    fm = st.segmenter({"id": "fm", "gate": "carrier"})
    assert fm.pre.maxlen == 500 // 32 and fm.pauses and fm.merge_frames == 0 and fm.dip_frames == 3
    st._gate = lambda r: Always()                                   # the voice gate needs torch; not here
    hf = st.segmenter({"id": "hf", "gate": "voice"})
    assert hf.pre.maxlen == 600 // 32 and hf.pauses is None and hf.merge_frames == 600 // 32
    tuned = st.segmenter({"id": "hf", "gate": "voice", "merge_gap_ms": 0, "preroll_ms": 300})
    assert tuned.merge_frames == 0 and tuned.pre.maxlen == 300 // 32
