"""The accurate pass with context (transcribe/context.py): stitching, window cutting, the hallucination guards, the
overlap merge and mapping words back to transmissions. The recogniser is a fake that 'hears' marker tones."""
import numpy as np

from transcribe import context as cx

SR = cx.SR


def tone(s, hz=1000, amp=0.3):
    t = np.arange(int(s * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def quiet(s):
    return np.zeros(int(s * SR), np.float32)


def test_stitch_joins_pause_split_pieces_and_separates_real_gaps():
    a = cx.Clip("a", 100.0, tone(3))
    b = cx.Clip("b", 103.0, tone(2))            # starts where a ended: a pause split
    c = cx.Clip("c", 110.0, tone(1))            # 5 s later on the air
    lay = cx.stitch([c, a, b])                  # any order in
    assert lay.order == ["a", "b", "c"]
    assert lay.spans["a"] == (0.0, 3.0)
    assert lay.spans["b"] == (3.0, 5.0)         # joined directly
    assert abs(lay.spans["c"][0] - (5.0 + cx.SEP_S)) < 1e-6
    assert [s for _, s in lay.breaks] == [1, 2]
    assert len(lay.audio) == int((6.0 + cx.SEP_S) * SR)


def test_stitch_tolerates_whole_second_start_times():
    # the log truncates starts to whole seconds: a piece logged 0.9 s "late" is still contiguous
    lay = cx.stitch([cx.Clip("a", 10.0, tone(2.4)), cx.Clip("b", 13.0, tone(1))])
    assert lay.breaks == [(2.4, 1)]


def test_plan_cuts_only_at_breaks_and_stays_under_the_window():
    clips, t = [], 0.0
    for i in range(12):                         # 12 transmissions of 5 s, 3 s apart
        clips.append(cx.Clip(str(i), t, tone(5))); t += 8
    lay = cx.stitch(clips)
    wins = cx.plan(lay)
    breaks = {round(b, 3) for b, _ in lay.breaks}
    assert wins[0][0] == 0 and abs(wins[-1][1] - len(lay.audio) / SR) < 1e-6
    for (a, b), (a2, _) in zip(wins, wins[1:]):
        assert b == a2 and b - a <= cx.WIN_MAX_S + 1e-6
        assert round(b, 3) in breaks            # never mid-transmission
    shifted = cx.plan(lay, phase=14)
    assert shifted[0][1] < wins[0][1]           # the second tiling cuts inside the first one's windows
    assert {round(b, 3) for _, b in shifted[:-1]}.isdisjoint({round(b, 3) for _, b in wins[:-1]})


def test_plan_cuts_a_long_transmission_at_its_longest_pause():
    x = np.concatenate([tone(14), quiet(0.2), tone(4), quiet(1.2), tone(14)])
    lay = cx.stitch([cx.Clip("long", 0.0, x)])
    wins = cx.plan(lay)
    assert len(wins) == 2
    assert 18.2 < wins[0][1] < 19.4            # inside the 1.2 s pause, not the 0.2 s one


def test_segment_guards():
    ok = {"text": " K7ABC, Bob in Springfield, no traffic.", "avg_logprob": -0.3, "no_speech_prob": 0.1, "compression_ratio": 1.2}
    assert cx.segment_ok(ok) == (True, "")
    assert cx.segment_ok({**ok, "compression_ratio": 3.1})[1] == "loop"
    assert cx.segment_ok({**ok, "text": " I'm a computer, I'm a computer, I'm a computer, I'm a computer"})[1] == "loop"
    assert cx.segment_ok({**ok, "text": " I'm a lawyer, I'm a tester, I'm a doctor, I'm a pilot, I'm a cook."})[1] == "loop"
    assert cx.segment_ok({**ok, "text": " ! ! ! ! ! ! ! ! ! ! H.R !.U. ! Mike"})[1] == "garbage"
    assert cx.segment_ok({**ok, "text": " Copy that! Over."})[0]
    assert cx.segment_ok({**ok, "no_speech_prob": 0.9, "avg_logprob": -0.9})[0]      # measured: dropping these lost more right words
    assert cx.segment_ok({**ok, "avg_logprob": -1.5})[0]
    assert cx.segment_ok({**ok, "text": " Radio net on 442.100 MHz. This is the evening net."},
                         prompt="Radio net on 442.100 MHz. This is the evening net.")[1] == "echo"
    assert cx.segment_ok({**ok, "text": " "})[1] == "empty"


def test_looped_keeps_real_repeated_digits():
    assert not cx.looped("K7 seven seven seven ABC".split())


def W(word, t0, t1, p=0.9, edge=99.0, ok=True):
    return {"w": word, "t0": t0, "t1": t1, "p": p, "edge": edge, "ok": ok, "why": "" if ok else "loop"}


def test_merge_takes_the_confident_side_where_hypotheses_differ():
    a = [W(" Kilo", 0, .3), W(" Seven", .3, .6, p=.3), W(" Bravo", .6, .9)]
    b = [W(" Kilo", 0, .3), W(" Eleven", .3, .6, p=.8), W(" Bravo", .6, .9)]
    assert [w["w"] for w in cx.merge([a, b])] == [" Kilo", " Eleven", " Bravo"]
    # a word right at a window cut counts for half
    b2 = [W(" Kilo", 0, .3), W(" Eleven", .3, .6, p=.5, edge=.2), W(" Bravo", .6, .9)]
    assert [w["w"] for w in cx.merge([a, b2])] == [" Kilo", " Seven", " Bravo"]


def test_a_looping_stretch_never_wins_on_probability():
    """Whisper rates its own repeats as very likely (2026-11-06, OpenVINO word probabilities): a repeated check-in or
    a character loop scores nothing in the merge."""
    say = "Delta Ray, Springfield, no traffic."
    a = [W(" " + w, i * .3, i * .3 + .3, p=.6) for i, w in enumerate(("Checking in " + say).split())]
    loop = [W(" " + w, i * .3, i * .3 + .3, p=.99) for i, w in enumerate(("Checking in " + say + " " + say + " " + say).split())]
    assert [w["w"].strip() for w in cx.merge([loop, a])] == ("Checking in " + say).split()
    junk = [W(" " + "-D-R-I-N-G-E" * 12, 0, 1, p=.99)]                       # as heard: ~130 characters
    real = [W(" Good", 0, .5, p=.5), W(" signal", .5, 1, p=.5)]
    assert [w["w"] for w in cx.merge([junk, real])] == [" Good", " signal"]


def fake_decoder(script):
    """'Hears' each 1 kHz tone burst in a window as the next word of `script` at the burst's time."""
    def dec(x, prompt=""):
        on = np.convolve(np.abs(x), np.ones(160) / 160, "same") > 0.1        # 10 ms envelope
        words, i = [], 0
        while i < len(on):
            if on[i]:
                j = i
                while j < len(on) and on[j]:
                    j += 1
                words.append((i / SR, j / SR))
                i = j
            else:
                i += 1
        return [{"text": "", "avg_logprob": -0.2, "no_speech_prob": 0.0, "compression_ratio": 1.0,
                 "words": [{"word": " " + script(a, b, x), "start": a, "end": b, "probability": 0.9} for a, b in words]}]
    return dec


def test_words_map_back_to_their_transmission_by_time():
    # three transmissions, each made of word bursts; the fake recogniser names each burst by its length
    def burst_words(n, w=0.25, gap=0.15):
        return np.concatenate([np.concatenate([tone(w + 0.05 * k), quiet(gap)]) for k in range(n)])
    a = np.concatenate([burst_words(3), quiet(0.8)])          # a pause split: the 0.8 s pause ends piece a
    clips = [cx.Clip("a", 0.0, a), cx.Clip("b", len(a) / SR, burst_words(2)), cx.Clip("c", 20.0, burst_words(4))]
    name = lambda a, b, x: f"w{round((b - a - 0.25) / 0.05)}"
    out = cx.transcribe_clips(clips, fake_decoder(name))
    assert out["a"]["text"] == "w0 w1 w2"
    assert out["b"]["text"] == "w0 w1"
    assert out["c"]["text"] == "w0 w1 w2 w3"
    assert out["a"]["words"][0][0] == "w0" and out["a"]["words"][0][1] < 0.05
    assert out["c"]["conf"] == 0.9 and out["c"]["flags"] == []
    # our own windows at two phases + the word merge give the same words here
    w = cx.transcribe_clips(clips, fake_decoder(name), mode="windows", phases=(0.0, 14.0))
    assert {k: v["text"] for k, v in w.items()} == {k: v["text"] for k, v in out.items()}


def test_a_word_timed_early_at_a_pause_split_goes_to_the_next_piece():
    # piece a = speech + the 0.8 s pause; piece b starts at the join with "This", which Whisper times 0.25 s early
    lay = cx.stitch([cx.Clip("a", 0.0, tone(3.0)), cx.Clip("b", 3.0, tone(2.0))])
    tiling = [{"words": [W(" over.", 1.6, 2.1), W(" This", 2.75, 2.95), W(" is", 3.0, 3.2)]}]
    r = cx.assemble(lay, [tiling])
    assert r["a"]["text"] == "over." and r["b"]["text"] == "This is"
    # at a real break (transmissions apart on the air) the clip edge stands
    lay = cx.stitch([cx.Clip("a", 0.0, tone(3.0)), cx.Clip("b", 10.0, tone(2.0))])
    tiling = [{"words": [W(" over.", 2.6, 2.9), W(" This", 3.6, 3.8)]}]
    r = cx.assemble(lay, [tiling])
    assert r["a"]["text"] == "over." and r["b"]["text"] == "This"


def test_targets_only_and_noise_phrase_and_empty():
    clips = [cx.Clip("ctx", 0.0, tone(1), target=False), cx.Clip("t", 2.0, tone(1)), cx.Clip("n", 9.0, quiet(2))]
    dec = lambda x, prompt="": [{"text": " Thank you.", "avg_logprob": -0.5, "no_speech_prob": 0.2, "compression_ratio": 1,
                                 "words": [{"word": " Thank", "start": 0.1, "end": 0.4, "probability": 0.5},
                                           {"word": " you.", "start": 0.4, "end": 0.7, "probability": 0.5}]}]
    out = cx.transcribe_clips(clips, dec, phases=(0.0,))
    assert set(out) == {"t", "n"}               # the context clip is decoded, not returned
    assert out["n"]["text"] == "" and "empty" in out["n"]["flags"]


def test_noise_phrase_dropped_when_unsure():
    lay = cx.stitch([cx.Clip("x", 0.0, tone(2))])
    tiling = [{"words": [W(" Thank", 0.2, 0.5, p=.4), W(" you.", 0.5, 0.8, p=.4)]}]
    r = cx.assemble(lay, [tiling])["x"]
    assert r["text"] == "" and "noise_phrase" in r["flags"]
    tiling = [{"words": [W(" Thank", 0.2, 0.5, p=.95), W(" you.", 0.5, 0.8, p=.95)]}]
    assert cx.assemble(lay, [tiling])["x"]["text"] == "Thank you."
    tiling = [{"words": [W(" jittery", 0.2, 0.5, p=.2), W(" plane", 0.5, 0.8, p=.25)]}]
    r = cx.assemble(lay, [tiling])["x"]
    assert r["text"] == "jittery plane" and r["flags"] == ["low_conf"]     # kept, flagged: the station sets it aside


def test_a_looped_segment_is_dropped_and_flagged():
    lay = cx.stitch([cx.Clip("x", 0.0, tone(4))])
    tiling = [{"words": [W(" good", 0.2, 0.5), W(" evening", 0.5, 0.8), W(" la", 1, 1.2, ok=False), W(" la", 1.2, 1.4, ok=False)]}]
    r = cx.assemble(lay, [tiling])["x"]
    assert r["text"] == "good evening" and r["flags"] == ["loop"]


def test_a_window_first_word_timed_early_does_not_jump_ahead_in_the_merge():
    """Real net on a laptop station (2026-11-07): OpenVINO timed a window's first word "Weather's" from 0.05 s, the
    other decode's "The" from 0.12 s; ordered by start the merge gave "Weather's The been real nice"."""
    lay = cx.stitch([cx.Clip("x", 0.0, tone(3.0))])
    # decode one's words sit near a window cut, so where the two agree ("weather's") decode two's copy is kept
    one = [{"words": [W(" The", 0.12, 0.56, edge=1.0), W(" weather's", 0.56, 1.06, edge=1.0),
                      W(" been", 1.06, 1.20, edge=1.0)]}]
    two = [{"words": [W(" Weather's", 0.05, 1.05), W(" been", 1.05, 1.19), W(" real", 1.19, 1.43), W(" nice.", 1.43, 1.83)]}]
    text = cx.assemble(lay, [one, two])["x"]["text"]
    assert text.lower().startswith("the weather's been") and "Weather's The" not in text
