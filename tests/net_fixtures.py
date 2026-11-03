"""Synthetic nets for the history / minutes tests: made-up example callsigns and places (Springfield), written to
read like Whisper output of a real directed net. Each line: (minutes after the start, text, callsigns, the minutes
items a careful human would note from it). Nothing here comes from a real log."""
from datetime import datetime, timedelta

ARES = [   # a weekly directed net, with business
    (0, "Good evening, this is K7NET, net control for the Springfield ARES Net. This is a directed net. "
        "Is there any emergency or priority traffic?", ["K7NET"], []),
    (1, "K7ABC, Springfield, no traffic.", ["K7ABC"], []),
    (1.5, "K7ABC, thank you, you're in the log. Next check-in.", ["K7ABC"], []),
    (2, "W9XYZ, Shelbyville, no traffic.", ["W9XYZ"], []),
    (3, "KD9AAA, mobile, with one item for the net.", ["KD9AAA"], []),
    (4, "KD9AAA, go ahead with your traffic.", ["KD9AAA"], []),
    (4.5, "Reminder, the Springfield hamfest is Saturday November 14th at 8 AM at the Springfield Fairgrounds.",
     [], ["announcement"]),
    (5, "VE testing is on the 21st at the Springfield Library, please register by November 18th.", [],
     ["announcement", "action"]),
    (6, "K7NET, roger. K7ABC will send the volunteer roster to everyone by Friday.", ["K7NET", "K7ABC"], ["action"]),
    (7, "We agreed to move the simulated emergency test to December 5th.", [], ["decision", "event"]),
    (8, "N0CALL, I have one formal message for the EOC.", ["N0CALL"], ["traffic"]),
    (9, "Volunteers are needed for the Thanksgiving fun run on November 26th, please let net control know.", [],
     ["action"]),
    (10, "Club meeting is Thursday at 7 PM at the Grange Hall.", [], ["announcement"]),
    (11, "There is no traffic tonight and no injuries reported from the exercise.", [], []),
    (12, "The fire department open house was fun.", [], []),
    (13, "W7TST, I'll be listening, good night.", ["W7TST"], []),
    (14, "The weather here is nice tonight, I'll stand by.", [], []),
    (15, "Is there any further business? This is K7NET closing the net. Seventy three.", ["K7NET"], []),
]

EMERGENCY = [   # an emergency net on a flood night
    (0, "This is K7NET, opening an emergency net for the flooding. Stations with emergency traffic call now.",
     ["K7NET"], ["urgent"]),
    (1, "W9XYZ, water is rising on Mill Creek Road, the road is closed at mile marker 12.", ["W9XYZ"], ["urgent"]),
    (2, "KD9AAA, we have an elderly woman missing near Riverside Park, last seen two hours ago.", ["KD9AAA"],
     ["urgent"]),
    (3, "K7ABC, power is out on the east side, about 200 homes.", ["K7ABC"], ["urgent"]),
    (4, "N0CALL, the Red Cross shelter at Springfield High School is open.", ["N0CALL"], ["urgent"]),
    (5, "KB7XYZ, requesting sandbags and water at the fire station on 5th Street.", ["KB7XYZ"], ["urgent"]),
    (6, "W7TST, no injuries here, all clear on the north side.", ["W7TST"], []),
    (7, "Any other emergency traffic for the net?", [], []),
    (8, "KD9AAA, a man fell and is bleeding, ambulance requested at 450 Oak Street.", ["KD9AAA"], ["urgent"]),
    (9, "K7NET, roger, all stations stand by.", ["K7NET"], []),
    (10, "K7NET, I will contact the county EOC about the shelter supplies.", ["K7NET"], ["action"]),
]

RAGCHEW = [
    (0, "CQ CQ this is W9XYZ.", ["W9XYZ"], []),
    (1, "The weather here is sunny, my QTH is Springfield.", [], []),
    (2, "I'll send you a QSL card next week.", [], ["action"]),
    (3, "I sat down to fix my antenna on Saturday.", [], []),
    (4, "Thanks for the contact. 73, W9XYZ clear.", ["W9XYZ"], []),
]


def lines(script, start: datetime, radio="ftm", freq=146.94, first_id=1000, ncs=None):
    """The script as day-log lines (what Station.finish writes), with a stated speaker where the text gives one."""
    from station import speaker
    out = []
    for i, (m, text, calls, _) in enumerate(script):
        t = start + timedelta(minutes=m)
        who, how = speaker.from_text(text, calls, ncs)
        out.append({"id": str(first_id + i), "radio": radio, "radio_name": radio.upper(), "freq_mhz": freq,
                    "start": t.isoformat(timespec="seconds"), "status": "final", "text": text, "callsigns": calls,
                    "speaker": who, "speaker_how": how, "terms": [], "duration_s": 4.0,
                    "wav": f"recordings/{t:%Y%m%d}/{first_id + i}_{radio}.wav"})
    return out


def checkin_net(start: datetime, calls: list[str], ncs="K7NET", radio="ftm", first_id=5000):
    """A short check-in net: net control opens, each station checks in and is acknowledged."""
    script = [(0, f"This is {ncs}, net control for the Springfield ARES Net.", [ncs], [])]
    for k, c in enumerate(calls):
        script.append((1 + k, f"{c}, Springfield, no traffic.", [c], []))
        script.append((1.5 + k, f"{c}, thank you, you're in the log.", [c], []))
    return lines(script, start, radio, first_id=first_id, ncs=ncs)


# Held out: written after the rules, never used to tune them. Its score is the honest one (test_minutes reports it).
HELDOUT = [
    (0, "Good evening all, this is W9XYZ standing in as net control for the Tuesday net tonight.", ["W9XYZ"], []),
    (1, "Before check-ins, a few announcements. The repeater will be off the air Saturday morning for antenna work.",
     [], ["announcement"]),
    (2, "Field Day planning meeting is next Monday at 6:30 PM at the Springfield Public Library.", [],
     ["announcement"]),
    (3, "Dues for next year are due by December 31st.", [], ["announcement"]),
    (4, "Now taking check-ins, please call now.", [], []),
    (5, "KB7XYZ, Springfield, no traffic.", ["KB7XYZ"], []),
    (5.5, "KB7XYZ, thanks for checking in.", ["KB7XYZ"], []),
    (6, "K7ABC, mobile on the interstate, I have a question about the parade.", ["K7ABC"], []),
    (7, "We need four more operators for the Veterans Day parade on November 11th.", [], ["action"]),
    (8, "K7ABC, I can do the parade, put me down for the north checkpoint.", ["K7ABC"], []),
    (9, "N0CALL, I'll bring the spare antenna and the go kit to the meeting.", ["N0CALL"], ["action"]),
    (10, "The board voted to buy a new duplexer for the repeater.", [], ["decision"]),
    (11, "AE7QQ, emergency traffic. There's a car accident on Highway 30 near mile marker 41, one person "
         "injured, fire and EMS are on scene.", ["AE7QQ"], ["urgent"]),
    (12, "AE7QQ, net control copies, do you need anything further?", ["AE7QQ"], []),
    (13, "Negative, no further assistance needed, they have it under control.", [], []),
    (14, "W7TST, short time, no traffic, I have to run to dinner.", ["W7TST"], []),
    (15, "The swap meet is the third Saturday of every month at the Grange.", [], ["announcement"]),
    (16, "Any late or missed check-ins?", [], []),
    (17, "Hearing none, this is W9XYZ closing the net at 19:42. Good night.", ["W9XYZ"], []),
]
