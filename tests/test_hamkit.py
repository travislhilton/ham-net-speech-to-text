"""hamkit: glossary, bands, DXCC prefixes, hints and net recognition."""
from datetime import datetime, timedelta

from hamkit import bands, dxcc, glossary, hints

CTY = """United States:            05:  08:  NA:   37.60:    91.87:     5.0:  K:
    AA,K,N,W,=4U1WB(5)[8];
Canada:                   05:  09:  NA:   44.35:    78.75:     5.0:  VE:
    VA,VE,VE3(4)[4];
Hawaii:                   31:  61:  OC:   21.12:   157.48:    10.0:  KH6:
    AH6,KH6,NH6,WH6;
Canary Islands:           33:  36:  AF:   28.32:    15.85:     0.0:  EA8:
    EA8,EC8;
"""


def test_glossary_forms_and_frequencies():
    t = [h["term"] for h in glossary.find("QRH? your signal drifts. Quebec Sierra Papa please. Q R T now")]
    assert t == ["QRH", "QSP", "QRT"]
    assert glossary.find("QRH?")[0]["means"].startswith("Does my frequency vary")
    assert [h["term"] for h in glossary.find("break break emergency traffic")] == ["BREAK BREAK", "EMERGENCY TRAFFIC"]
    assert [h["term"] for h in glossary.find("seventy three, good night")] == ["73"]
    assert glossary.find("the repeater on 146.73 and 146.88") == []        # frequencies, not sign-offs
    assert [h["term"] for h in glossary.find("thanks 73")] == ["73"]


def test_bands():
    assert bands.band(146.94)["band"] == "2 m" and bands.band(146.94)["usual_offset_mhz"] == -0.6
    assert bands.band(147.12)["usual_offset_mhz"] == 0.6
    assert bands.band(14.230)["label"] == "14.230 MHz (20 m)" and bands.band(14.230)["range"] == "HF"
    assert bands.band(446.0)["band"] == "70 cm" and "repeater_output" not in bands.band(446.0)
    assert bands.band(162.55)["band"] is None and bands.band(None) is None


def test_dxcc(tmp_path):
    p = tmp_path / "cty.dat"; p.write_text(CTY)
    dxcc._db.cache_clear()
    look = lambda c: dxcc.lookup(c, str(p))
    assert look("EA8AB")["country"] == "Canary Islands" and look("EA8AB")["lon"] == -15.85
    assert look("VE3/W9XYZ")["country"] == "Canada" and look("VE3/W9XYZ")["cq"] == 4
    assert look("W9XYZ/KH6")["country"] == "Hawaii"
    assert look("W9XYZ/P")["country"] == "United States"
    assert look("4U1WB")["matched"] == "=4U1WB"
    assert look("ZZ9ZZ") is None
    dxcc._db.cache_clear()


def test_relay_card_and_identify_reminder():
    h = hints.Hinter("W9XYZ")
    t0 = datetime(2026, 11, 2, 19, 0)
    r = h.on_transmission("Net control, I'm unable to copy K7ABC, can anyone relay?", ["K7ABC"], t0)
    assert r["cards"][0]["key"] == "relay"
    assert "W9XYZ, I can relay." in r["cards"][0]["say"]
    h.on_transmission("Whiskey Nine X-ray Yankee Zulu checking in, no traffic", ["W9XYZ"], t0)
    assert h.on_transmission("go ahead", [], t0 + timedelta(minutes=5))["reminders"] == []
    rem = h.on_transmission("go ahead", [], t0 + timedelta(minutes=9, seconds=30))["reminders"]
    assert rem and "Identify soon" in rem[0]["text"]


def test_check_in_card_uses_phonetics():
    r = hints.Hinter("W9XYZ").on_transmission("Any check-ins for the net?", [])
    assert r["cards"][0]["say"][0].startswith("Whiskey Nine X-ray Yankee Zulu")


def test_net_recognition_emergency_wins():
    h = hints.Hinter()
    h.on_transmission("This is net control for the Springfield Valley ARES net, taking check-ins", [])
    assert h.classify()["type"] in ("ares_races", "directed", "check_in")
    r = h.on_transmission("Break break, emergency traffic, there is a fire at the church", [])
    assert r["net"]["type"] == "emergency" and r["cards"][0]["key"] == "emergency"
    ex = hints.Hinter(); ex.on_transmission("This is an exercise, this is an exercise", [])
    assert ex.classify()["type"] == "exercise"
