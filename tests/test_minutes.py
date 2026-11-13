import pytest
"""Minutes drafted by rules (station/minutes.py): precision/recall on hand-labelled synthetic nets, and the pieces."""
from datetime import date, datetime

from station import minutes
from tests.net_fixtures import ARES, EMERGENCY, HELDOUT, RAGCHEW, lines

TUE = datetime(2026, 10, 27, 19, 0)


def score(script, emergency=False):
    views = lines(script, TUE)
    got = minutes.extract(views, TUE.date(), emergency)
    by_tx = {}
    for it in got:
        by_tx.setdefault(it["tx_id"], set()).add(it["kind"])
    tp = fp = fn = 0
    per: dict[str, list[int]] = {}
    for v, (_, _, _, want) in zip(views, script):
        pred, want = by_tx.get(v["id"], set()), set(want)
        for k in pred | want:
            c = per.setdefault(k, [0, 0, 0])
            c[0] += k in pred and k in want; c[1] += k in pred and k not in want; c[2] += k in want and k not in pred
        tp += len(pred & want); fp += len(pred - want); fn += len(want - pred)
    return tp, fp, fn, per


def test_precision_and_recall_on_fixture_nets(capsys):
    total = [0, 0, 0]
    report = {}
    for name, script, em in (("ares", ARES, False), ("emergency", EMERGENCY, True), ("ragchew", RAGCHEW, False)):
        tp, fp, fn, per = score(script, em)
        total = [total[0] + tp, total[1] + fp, total[2] + fn]
        for k, c in per.items():
            r = report.setdefault(k, [0, 0, 0])
            for i in range(3):
                r[i] += c[i]
    p, r = total[0] / (total[0] + total[1]), total[0] / (total[0] + total[2])
    with capsys.disabled():
        print(f"\nminutes extraction on the fixture nets: precision {p:.2f}, recall {r:.2f} "
              + ", ".join(f"{k} {c[0]}/{c[0] + c[1]} right, {c[0]}/{c[0] + c[2]} found" for k, c in sorted(report.items())))
    assert p >= 0.9 and r >= 0.85
    assert report["urgent"][1] == 0                                        # no false alarm on the urgent board


def test_details_of_an_item():
    got = {i["text"][:20]: i for i in minutes.extract(lines(ARES, TUE), TUE.date())}
    ham = got["Reminder, the Spring"]
    assert ham["event_at"] == "2026-11-14T08:00" and ham["place"] == "Springfield Fairgrounds"
    ve = [i for i in minutes.extract(lines(ARES, TUE), TUE.date()) if i["text"].startswith("VE testing")]
    assert {i["kind"] for i in ve} == {"announcement", "action"} and ve[1]["due"] == "2026-11-18"
    roster = got["K7ABC will send the "]
    assert roster["kind"] == "action" and roster["who"] == "K7ABC" and roster["due"] == "2026-10-30"
    vol = got["Volunteers are neede"]
    assert vol["kind"] == "action" and "who" not in vol and vol["by"] is None      # a request nobody has taken yet
    club = got["Club meeting is Thur"]
    assert club["event_at"] == "2026-10-29T19:00" and club["place"] == "Grange Hall"


def test_urgent_details_and_denials():
    got = [i for i in minutes.extract(lines(EMERGENCY, TUE), TUE.date(), emergency=True) if i["kind"] == "urgent"]
    road = next(i for i in got if "Mill Creek" in i["text"])
    assert road["category"] == "flood" and set(road["categories"]) >= {"flood", "roads"}
    assert road["place"] == "mile marker 12" or "Mill Creek Road" in road["place"]
    missing = next(i for i in got if "missing" in i["text"])
    assert missing["category"] == "missing person" and "elderly woman" in missing["people"]
    hurt = next(i for i in got if "bleeding" in i["text"])
    assert hurt["place"] == "450 Oak Street"
    # on a net that is not an emergency, a weak cue alone (power, shelter...) needs a place before it is urgent
    assert not minutes.extract([{"id": "1", "start": "2026-10-27T19:00", "text": "My power is out again.",
                                 "callsigns": []}], TUE.date())
    for s in ("No injuries and no damage to report.", "Any emergency traffic?", "Stations with emergency traffic, "
              "call now.", "The fire department open house is Saturday.", "Is anyone missing a glove?"):
        assert not [i for i in minutes.extract([{"id": "1", "start": "2026-10-27T19:00", "text": s, "callsigns": []}],
                                               TUE.date(), emergency=True) if i["kind"] == "urgent"], s


def test_dates_and_times():
    ref = date(2026, 10, 27)                                               # a Tuesday
    f = lambda s: minutes.find_date(s, ref)[0]
    assert f("on Saturday") == date(2026, 10, 31) and f("next Tuesday") == date(2026, 11, 3)
    assert f("tomorrow night") == date(2026, 10, 28) and f("tonight") == ref
    assert f("November 14th") == date(2026, 11, 14) and f("the 3rd of January") == date(2027, 1, 3)
    assert f("on 12/5") == date(2026, 12, 5) and f("the 21st") == date(2026, 11, 21) - date.resolution * 0 \
        or f("the 21st") == date(2026, 11, 21)
    assert f("the 30th") == date(2026, 10, 30)
    assert f("I sat in the sun") is None and f("you may check in") is None          # not weekdays or months
    assert minutes.find_time("at 7 PM") == "19:00" and minutes.find_time("at 7:30 a.m.") == "07:30"
    assert minutes.find_time("1900 hours") == "19:00" and minutes.find_time("starts at 18:45") == "18:45"
    assert minutes.find_time("146.940") is None and minutes.find_time("at noon") == "12:00"


def test_still_valid():
    t = date(2026, 11, 3)
    assert minutes.still_valid({"until": "2026-11-05"}, t) == (True, "until 2026-11-05")
    assert minutes.still_valid({"event_at": "2026-11-01T08:00"}, t)[0] is False
    assert minutes.still_valid({}, t, date(2026, 10, 27))[0] is True
    assert minutes.still_valid({}, t, date(2026, 9, 1))[0] is False


def test_held_out_net(capsys):
    """HELDOUT was written after the rules. First measured 2026-11-03: precision 7/9 = 0.78, recall 7/8 = 0.88
    (misses: "please call now" taken for a request; "four more operators" not read as one). Both rules were then
    fixed, so this net is no longer unseen; the bar below is the first measurement, not the tuned one."""
    tp, fp, fn, _ = score(HELDOUT)
    with capsys.disabled():
        print(f"\nheld-out net: precision {tp}/{tp + fp}, recall {tp}/{tp + fn}")
    assert tp / (tp + fp) >= 0.75 and tp / (tp + fn) >= 0.85


def test_a_garbled_fragment_is_not_an_urgent_fire_outside_an_emergency_net():
    from station.minutes import extract
    v = [{"id": "1", "start": "2026-11-02T20:04:31", "text": "He's burning river!", "speaker": None, "callsigns": []}]
    assert not [i for i in extract(v) if i["kind"] == "urgent"]
    v[0]["text"] = "There is a house burning on Main Street, two people outside."
    assert [i["category"] for i in extract(v) if i["kind"] == "urgent"] == ["fire"]


def test_a_bare_alarm_word_on_a_routine_net_is_not_urgent():
    """Owner 2026-11-03: banners fired on a fire station's tactical name and a check-in with a mis-heard word."""
    from station.minutes import extract
    u = lambda s, em=False: [i["category"] for i in extract([{"id": "1", "start": "2026-11-03T21:00", "text": s,
                                                               "callsigns": []}], emergency=em) if i["kind"] == "urgent"]
    for s in ("Now calling a lad fire.", "Ambulance, outstanding signal, and welcome to the net.",
              "This is K7ABC checking in for the Springfield Fire Station", "This is K7ABC checking in from Springfield Fire.",
              "Springfield Fire, go ahead.", "Calling Springfield fire, Springfield fire.", "Thank you, Springfield Fire. Good signal.",
              "Shelbyville Fire checking in with no traffic.", "Fire.", "Next on the list, Ambulance."):
        assert u(s) == [], s
    for s, cat in (("There is a structure fire at 450 Oak Street.", "fire"), ("The house is on fire.", "fire"),
                   ("We have a brush fire spreading toward the highway.", "fire"), ("There's a fire on Main Street.", "fire"),
                   ("We need an ambulance at the school.", "injury"), ("Someone is not breathing here.", "injury"),
                   ("Smoke coming from the hillside behind the church.", "fire"), ("Fire reported near the canyon mouth.", "fire"),
                   ("Springfield Fire is responding to a structure fire on Elm.", "fire"), ("Mayday mayday.", "emergency")):
        assert u(s) == [cat], s
    assert u("Fire.", em=True) == ["fire"]                     # an emergency net stays as sensitive as before


def _urgent(text):
    return [i for i in minutes.extract([{"id": "1", "start": "2026-11-12T18:00:00", "text": text}]) if i["kind"] == "urgent"]


@pytest.mark.parametrize("text", [
    "He has a nice big old fire tower in his backyard.",                 # a thing: fire + noun
    "I wanted you to have a fire transducer would show it would send 50 volts.",
    "fire seat, if there are big cars around there, is in the same shape.",  # "there are" is not about the fire
    "Uh, Woo, a loud fire.",                                              # a mis-heard word, nothing reported
    "I thought there would be a fire at me, Eugene.",                     # "at me" is not a place
    "Let me fire up the rig and I'll call you back.",                     # the verb
    "I'll fire off an email to the EC tonight.",
    "I just blocked and reported the spam.",                               # "blocked": not a road
    "You need to take up your beverage antennas before they crash.",
])
def test_ordinary_talk_is_not_urgent(text):
    assert _urgent(text) == []


@pytest.mark.parametrize("text", [
    "There's a structure fire on Maple Street, engines responding.",
    "We have a brush fire spreading toward the highway.",
    "Fire is burning behind the school.",
    "Reported fire at the grain elevator north of town.",
    "The house is on fire and people are still inside.",
    "We've got a fire near the north gate.",
    "There's a fire.",
    "A barn is burning on County Road 5.",
    "255 EMS personnel are on scene.",
])
def test_a_reported_fire_or_incident_is_urgent(text):
    assert _urgent(text)


@pytest.mark.parametrize("text,urgent", [
    ("You know, I haven't heard from you for a while.", False),           # a greeting (a station, 11-06)
    ("Good to hear you, haven't heard from ya in ages.", False),
    ("I got injured like six years ago.", False),                         # a story (a station, 11-10)
    ("Back in 2019 we had a house fire on our street, everybody got out.", False),
    ("We haven't heard from his mother since the flood came through town.", True),
    ("A hiker was injured two hours ago near the trailhead and needs help.", True),
    ("155 EMS personnel are on scene.", True),
])
def test_greetings_and_old_stories_are_not_urgent(text, urgent):
    got = [i for i in minutes.extract([{"id": "t", "start": "2026-11-12T20:00:00", "text": text, "speaker": "K7ABC"}])
           if i["kind"] == "urgent"]
    assert bool(got) is urgent, got


def test_a_cue_spelled_into_a_call_sign_is_not_urgent():
    roll = "And I want to thank Tony, K27-CPR-Block, IC-CAM-NIC-CIR, KQ7-EMS, this is K7ABC."
    assert minutes.urgent_categories(roll) == []
    assert minutes.urgent_categories("He needed CPR at the trailhead, the ambulance is on scene.") != []
