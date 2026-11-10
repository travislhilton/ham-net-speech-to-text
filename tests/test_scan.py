"""The HF scan: bands by hour and antenna, peaks above the floor, parking on a conversation, nets first,
the operator's dial wins, and remembered spots."""
from datetime import timedelta

import pytest

from station import cat, scan
from station.scan import Spots, bands_now, candidates
from tests.test_cat import FakeIC705, Clock, TUE, station


class Rig(FakeIC705):
    """Answers the S-meter: a strong signal near 7.200 MHz, noise elsewhere."""
    signal_at = 7.200

    def write(self, b):
        if b[4] == 0x15 and b[5] == 0x02:
            self.sent.append(bytes(b))
            if self.echo:
                self.out += b
            lvl = 150 if abs(self.freq / 1e6 - self.signal_at) < 0.002 else 30
            self.out += bytes([0xFE, 0xFE, 0xE0, self.civ, 0x15, 0x02]) + bytes.fromhex(f"{lvl:04d}") + b"\xFD"
            return
        super().write(b)


def test_bands_follow_the_hour_and_the_antenna():
    assert bands_now(21, ["80", "40", "20"]) == ["40", "80", "20"]
    assert bands_now(12, ["40", "20"]) == ["20", "40"]
    assert bands_now(12, ["160"]) == []


def test_candidates_are_peaks_above_the_floor_one_per_6_khz():
    lv = [(7.125 + i * 0.003, 30) for i in range(40)]
    lv[10] = (lv[10][0], 160); lv[11] = (lv[11][0], 150); lv[30] = (lv[30][0], 70); lv[5] = (lv[5][0], 45)
    c = candidates(lv)
    assert [round(f, 3) for f, _ in c] == [round(lv[10][0], 3), round(lv[30][0], 3)]


def test_spots_rank_by_hour_and_obey_pin_hide_forget(tmp_path):
    s = Spots(tmp_path / "s.json")
    s.hit(7.188, "LSB", TUE.replace(hour=20), ["K7ABC"])
    s.hit(14.290, "USB", TUE.replace(hour=12))
    assert s.ranked(20)[0]["freq_mhz"] == 7.188 and s.ranked(12)[0]["freq_mhz"] == 14.29
    s.act("14.290 USB", "pin")
    assert s.ranked(20)[0]["freq_mhz"] == 14.29
    s.act("14.290 USB", "hide")
    assert [x["freq_mhz"] for x in s.ranked(20)] == [7.188]
    s.act("7.188 LSB", "forget")
    assert Spots(tmp_path / "s.json").ranked(20) == []


def test_the_scan_never_sends_ptt():
    with pytest.raises(cat.CatError):
        cat.frame(0xA4, 0x1C, 0x00, b"\x01")
    assert (0x15, 0x02) in cat.ALLOWED and not any(k[0] == 0x1C for k in cat.ALLOWED)


def test_a_sweep_finds_the_signal_parks_and_remembers_the_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "SETTLE_S", 0)
    monkeypatch.setattr(scan, "STAY_QUIET_S", -1)
    monkeypatch.setattr(scan, "BANDS", {"40": (7.190, 7.210, "LSB")})
    radio, clock = Rig(freq=7_150_000, mode=0x00), Clock(TUE.replace(hour=21, minute=30))
    st = station(tmp_path, radio, clock, antenna="hf")
    st.scan.set_antenna("ic", {"name": "Inverted V", "bands": ["40"]})
    st.scan.set_mode("ic", "on")
    st.tx.append({"id": "x", "radio": "ic", "status": "final", "start": clock().isoformat(timespec="seconds"),
                  "text": "K7ABC this is N0CALL good signal here", "callsigns": ["K7ABC"], "noise": False})
    st.scan.cycle("ic")
    spots = st.scan.spots.ranked(21)
    assert spots and spots[0]["freq_mhz"] == pytest.approx(7.2, abs=0.002) and "K7ABC" in spots[0]["calls"]
    assert st.radios["ic"]["scan"]["state"] == "conversation"
    assert not any(f[4] == 0x1C for f in radio.sent)                        # receive only


def test_a_net_comes_first_and_the_operators_dial_wins(tmp_path):
    radio, clock = Rig(freq=7_150_000, mode=0x00), Clock(TUE.replace(hour=19, minute=56))
    st = station(tmp_path, radio, clock, antenna="hf")
    st.scan.set_mode("ic", "on")
    st.radios["ic"]["listen"] = {"next": {"name": "Example net", "start": TUE.replace(hour=20).isoformat()}}
    ok, why = st.scan.allowed("ic", clock())
    assert not ok and "net first" in why
    st.radios["ic"]["listen"] = {}
    assert st.scan.allowed("ic", clock())[0]
    rig = st.cat._rig("ic")
    radio.freq = 7_230_000                                                 # the operator turns the dial
    assert st.scan._touched("ic", rig, 7.150)
    ok, why = st.scan.allowed("ic", clock())
    assert not ok and "You tuned" in why
    clock.t = clock.t + timedelta(minutes=31)
    assert st.scan.allowed("ic", clock())[0]


def test_scan_refuses_a_radio_that_cannot(tmp_path):
    radio, clock = Rig(), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock, antenna="vu")
    with pytest.raises(ValueError):
        st.scan.set_mode("ic", "on")
    assert "VHF/UHF" in st.scan.can("ic")


def test_the_scan_keeps_to_the_operators_phone_privileges(tmp_path, monkeypatch):
    """A General scans only General phone: 3.800-4.000, 7.175-7.300, 14.225-14.350, 21.275-21.450 (owner 11-10)."""
    from station.scan import segment
    assert segment("80", "general")[:2] == (3.8, 4.0) and segment("40", "general")[:2] == (7.175, 7.3)
    assert segment("20", "general")[:2] == (14.225, 14.35) and segment("15", "general")[:2] == (21.275, 21.45)
    assert segment("17", "general")[:2] == (18.11, 18.168) and segment("10", "general")[:2] == (28.3, 28.85)
    assert segment("40", "extra")[:2] == (7.125, 7.3) and segment("40", None)[:2] == (7.125, 7.3)
    assert segment("80", "technician") is None and segment("10", "technician")[:2] == (28.3, 28.5)
    monkeypatch.setattr(scan, "SETTLE_S", 0)
    monkeypatch.setattr(scan, "STAY_QUIET_S", -1)
    monkeypatch.setattr(scan, "BANDS", {"40": (7.140, 7.190, "LSB")})
    radio, clock = Rig(freq=7_150_000, mode=0x00), Clock(TUE.replace(hour=21, minute=30))
    radio.signal_at = 7.150                                                    # a conversation in Extra-only phone
    st = station(tmp_path, radio, clock, antenna="hf")
    st.agent.cfg["license_class"] = "general"
    st.scan.spots.hit(7.150, "LSB", clock(), ["K7ABC"])                        # remembered from before: not tried
    st.scan.spots.hit(7.150, "LSB", clock(), ["K7ABC"])
    st.scan.set_antenna("ic", {"name": "Inverted V", "bands": ["40"]})
    st.scan.set_mode("ic", "on")
    st.scan.cycle("ic")
    assert st.scan.view()["bands"]["40"]["from"] == 7.175 and st.scan.view()["license_class"] == "general"
    assert radio.freq / 1e6 >= 7.175 - 1e-9                                   # never parked below General phone
    assert st.radios["ic"]["scan"]["state"] != "conversation"
