"""CAT control of an Icom radio (station/cat.py): the CI-V wire format, a simulated IC-705 (with USB echo-back), the
station reading the radio's frequency, tuning it for a scheduled net (VHF/UHF and HF alike) and putting it back after,
and the guard that it can never transmit."""
import copy
import time

import pytest

from station import cat
from station import engine
from station.cat import CatControl, CatError, Icom
from station.core import Station
from station.schedule import validate
from tests.test_schedule import TUE, Clock


class FakeIC705:
    """A CI-V radio on a serial port: echoes every frame (CI-V USB echo back ON), answers reads, FB to sets."""

    def __init__(self, civ=0xA4, freq=7_185_000, mode=0x00, echo=True, answer=True):
        self.civ, self.freq, self.mode, self.data = civ, freq, mode, 0
        self.duplex, self.offset, self.tone, self.tone_on, self.tsql = 0x10, b"", b"", 0, 1
        self.echo, self.answer = echo, answer
        self.out = bytearray()
        self.sent = []                                         # every frame the station wrote
        self.closed = False

    def write(self, b):
        self.sent.append(bytes(b))
        if self.echo:
            self.out += b
        if not self.answer:
            return
        assert b[:2] == b"\xFE\xFE" and b[2] == self.civ and b[3] == 0xE0 and b[-1:] == b"\xFD"
        cmd, rest = b[4], b[5:-1]
        reply = lambda payload: self.out.extend(bytes([0xFE, 0xFE, 0xE0, self.civ]) + payload + b"\xFD")
        ok = lambda: reply(b"\xFB")
        if cmd == 0x03:
            reply(b"\x03" + cat.bcd_freq(self.freq))
        elif cmd == 0x04:
            reply(bytes([0x04, self.mode, 0x01]))
        elif cmd == 0x05:
            self.freq = cat.freq_bcd(rest); ok()
        elif cmd == 0x06:
            self.mode = rest[0]; ok()
        elif cmd == 0x0F:
            self.duplex = rest[0]; ok()
        elif cmd == 0x0D:
            self.offset = rest; ok()
        elif cmd == 0x1B and rest[0] == 0x00:
            self.tone = rest[1:]; ok()
        elif cmd == 0x16 and rest[0] == 0x42:
            self.tone_on = rest[1]; ok()
        elif cmd == 0x16 and rest[0] == 0x43:
            self.tsql = rest[1]; ok()
        elif cmd == 0x1A and rest[:1] == b"\x06":
            if len(rest) == 1:
                reply(bytes([0x1A, 0x06, self.data, 0]))
            else:
                self.data = rest[1]; ok()
        else:
            reply(b"\xFA")

    def read(self, n):
        b = bytes(self.out[:n]); del self.out[:n]
        return b

    def close(self):
        self.closed = True

    @property
    def mhz(self):
        return self.freq / 1e6


# ----- the wire format -----
def test_bcd_frequency_offset_and_tone():
    assert cat.bcd_freq(146_720_000) == bytes([0x00, 0x00, 0x72, 0x46, 0x01])
    assert cat.freq_bcd(cat.bcd_freq(3_915_000)) == 3_915_000 and cat.freq_bcd(cat.bcd_freq(449_650_000)) == 449_650_000
    assert cat.bcd_offset(-5) == bytes([0x00, 0x00, 0x05]) and cat.bcd_offset(0.6) == bytes([0x00, 0x60, 0x00])
    assert cat.bcd_tone(88.5) == bytes([0x00, 0x08, 0x85]) and cat.bcd_tone(103.5) == bytes([0x00, 0x10, 0x35])


def test_it_can_never_transmit():
    with pytest.raises(CatError):
        cat.frame(0xA4, 0x1C, 0x00, b"\x01")                  # PTT on
    rig = Icom(FakeIC705())
    with pytest.raises(CatError):
        rig.ask(0x1C, 0x00, b"\x01")
    assert rig.t.sent == []                                    # nothing reached the port


def test_the_port_opens_with_rts_and_dtr_low():
    """Opening a COM port raises RTS/DTR by default; a radio set to key on them would transmit."""
    seen = {}

    class S:
        def open(self):
            seen.update(rts=self.rts, dtr=self.dtr, port=self.port)
    cat.serial_port("COM11", 19200, serial_mod=type("m", (), {"Serial": S}))
    assert seen == {"rts": False, "dtr": False, "port": "COM11"}


def test_parse_skips_noise_and_keeps_a_partial_frame():
    buf = bytearray(b"\x00\x11\xFE\xFE\xE0\xA4\xFB\xFD\xFE\xFE\xE0")
    assert cat.parse(buf) == [b"\xFE\xFE\xE0\xA4\xFB\xFD"] and bytes(buf) == b"\xFE\xFE\xE0"


def test_reads_past_the_echo_and_tunes_fm_with_tone_squelch_off():
    radio = FakeIC705()
    rig = Icom(radio)
    assert rig.frequency() == pytest.approx(7.185) and rig.mode() == "LSB"
    rig.tune(442.1, "FM", offset_mhz=-5, tone_hz=100.0)
    assert radio.mhz == pytest.approx(442.1) and radio.mode == 0x05 and radio.tsql == 0
    assert radio.duplex == 0x11 and radio.offset == bytes([0, 0, 5]) and radio.tone == bytes([0x00, 0x10, 0x00])
    assert radio.tone_on == 1
    rig.tune(3.915, "LSB")                                     # HF just as well
    assert radio.mhz == pytest.approx(3.915) and radio.mode == 0x00 and radio.data == 0
    rig.tune(14.105, "DIG")
    assert radio.mode == 0x01 and radio.data == 1 and rig.mode() == "DIG"


def test_coverage_and_a_silent_radio():
    rig = Icom(FakeIC705())
    assert rig.covers(146.94) and rig.covers(442.1) and rig.covers(3.915) and not rig.covers(222.1)
    with pytest.raises(CatError, match="outside"):
        rig.tune(1296.0, "FM")
    mute = Icom(FakeIC705(answer=False))
    cat.REPLY_S, keep = 0.2, cat.REPLY_S
    try:
        with pytest.raises(CatError, match="no answer"):
            mute.frequency()
    finally:
        cat.REPLY_S = keep


def test_schedule_keeps_a_nets_offset_and_tone():
    v = validate({"name": "Repeater net", "radio": "ic", "freq_mhz": "442.100", "mode": "fm", "days": ["Tue"],
                  "start": "20:00", "offset_mhz": "-5", "tone_hz": "100.0"}, {"ic": {}})
    assert v["offset_mhz"] == -5 and v["tone_hz"] == 100.0
    with pytest.raises(ValueError, match="tone"):
        validate({"name": "x", "radio": "ic", "days": ["Tue"], "start": "20:00", "tone_hz": "7"}, {"ic": {}})


# ----- in the station -----
CFG = {"callsign": "W9XYZ",
       "radios": [{"id": "ic", "name": "IC-705", "device": "IC-705", "gate": "voice", "freq_mhz": 7.185,
                   "mode": "LSB", "cat": {"port": "COM11", "civ": "A4"}}],
       "schedule": [{"name": "Example repeater net", "radio": "ic", "freq_mhz": 442.1, "mode": "FM", "days": ["Tue"],
                     "start": "20:00", "duration_min": 60, "offset_mhz": -5, "tone_hz": 100.0},
                    {"name": "Example HF net", "radio": "ic", "freq_mhz": 3.915, "mode": "LSB", "days": ["Wed"],
                     "start": "19:00", "duration_min": 30}],
       "port": 0, "bind": "127.0.0.1", "qrz_online": False}


def station(tmp_path, radio, clock, antenna="vu"):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=clock)
    st.cat = CatControl(st, lambda c: Icom(radio, c["civ"], c["model"]))
    if antenna:
        st.set_radio_setup("ic", {"antenna": antenna})
    return st


def edge(st, clock, t):
    """One schedule tick at `t`; wait for the CAT thread it started."""
    clock.t = t
    before = {th.name for th in __import__("threading").enumerate()}
    st.nets.tick()
    for th in __import__("threading").enumerate():
        if th.name.startswith("cat-") and th.name not in before or th.name == "cat-ic":
            th.join(5)


def test_the_station_reads_the_radio_and_tunes_it_for_a_vhf_net_then_puts_it_back(tmp_path):
    radio, clock = FakeIC705(freq=7_185_000, mode=0x00), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock)
    radio.freq = 7_200_000
    assert st.cat.poll("ic") == (pytest.approx(7.2), "LSB")
    assert st.radios["ic"]["freq_mhz"] == 7.2 and st.radios["ic"]["cat"]["ok"] is True      # the card follows
    edge(st, clock, TUE.replace(hour=19, minute=59))                     # the net's window opens (1 min lead)
    assert radio.mhz == pytest.approx(442.1) and radio.mode == 0x05 and radio.duplex == 0x11 and radio.tsql == 0
    assert st.radios["ic"]["freq_mhz"] == 442.1 and st.radios["ic"]["cat"]["tuned_for"] == "Example repeater net"
    assert st._saved()["radios"]["ic"]["cat_restore"]["freq_mhz"] == pytest.approx(7.2)  # survives a restart
    edge(st, clock, TUE.replace(hour=21, minute=6))                      # window closed: put back
    assert radio.mhz == pytest.approx(7.2) and radio.mode == 0x00
    assert st.radios["ic"]["freq_mhz"] == 7.2 and "cat_restore" not in st._saved()["radios"]["ic"]
    assert not any(f[4] == 0x1C for f in radio.sent)                     # never a PTT frame


def test_with_the_hf_antenna_on_a_uhf_net_is_not_tuned_to(tmp_path):
    radio, clock = FakeIC705(freq=7_185_000, mode=0x00), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock, antenna=None)                  # an IC-705 starts on its HF antenna
    assert st.radios["ic"]["antenna"] == "hf"
    assert st.nets.statuses()[st.cfg["schedule"][0]["id"]]["kind"] == "cant"
    edge(st, clock, TUE.replace(hour=19, minute=59))
    assert radio.mhz == pytest.approx(7.185) and "cat_restore" not in st._saved().get("radios", {}).get("ic", {})


def test_an_hf_net_is_tuned_the_same_way(tmp_path):
    radio, clock = FakeIC705(freq=146_520_000, mode=0x05), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock, antenna="hf")
    edge(st, clock, TUE.replace(day=4, hour=18, minute=59))              # Wednesday's HF net
    assert radio.mhz == pytest.approx(3.915) and radio.mode == 0x00
    edge(st, clock, TUE.replace(day=4, hour=19, minute=36))
    assert radio.mhz == pytest.approx(146.52) and radio.mode == 0x05


def test_the_operator_retuning_during_the_net_is_left_alone(tmp_path):
    radio, clock = FakeIC705(), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock)
    edge(st, clock, TUE.replace(hour=19, minute=59))
    radio.freq = 446_000_000                                              # the operator turned the dial
    edge(st, clock, TUE.replace(hour=21, minute=6))
    assert radio.mhz == pytest.approx(446.0) and "cat_restore" not in st._saved()["radios"]["ic"]


def test_a_restart_inside_the_net_keeps_what_to_put_back(tmp_path):
    radio, clock = FakeIC705(freq=7_185_000), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock)
    edge(st, clock, TUE.replace(hour=19, minute=59))
    st2 = station(tmp_path, radio, clock)                                 # restarted at 20:30, mid-net
    edge(st2, clock, TUE.replace(hour=20, minute=30))
    assert st2._saved()["radios"]["ic"]["cat_restore"]["freq_mhz"] == pytest.approx(7.185)
    edge(st2, clock, TUE.replace(hour=21, minute=6))
    assert radio.mhz == pytest.approx(7.185)


def test_change_on_the_card_tunes_a_cat_radio_and_a_missing_port_says_why(tmp_path):
    radio, clock = FakeIC705(), Clock(TUE.replace(hour=12))
    st = station(tmp_path, radio, clock)
    r = st.cat.tune_now("ic", "146,94", "FM")
    assert radio.mhz == pytest.approx(146.94) and r["freq_mhz"] == 146.94
    with pytest.raises(ValueError, match="outside"):
        st.cat.tune_now("ic", "1296.1", "FM")

    def no_port(c):
        raise OSError("could not open port 'COM11'")
    st3 = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path / "b", clock=clock)
    st3.cat = CatControl(st3, no_port)
    assert st3.cat.poll("ic") is None
    assert st3.radios["ic"]["cat"]["ok"] is False and "COM11" in st3.radios["ic"]["cat"]["error"]
    with pytest.raises(ValueError, match="CAT"):
        st3.cat.tune_now("ic", 146.94, "FM")


def test_radios_dialog_keeps_a_valid_cat_port_and_refuses_a_bad_one():
    from station import radios
    out = radios.clean([{"name": "IC-705", "device": "IC-705", "gate": "voice", "mode": "LSB",
                         "cat": {"port": "com11", "civ": "a4"}}])
    assert out[0]["cat"] == {"port": "COM11", "civ": "A4", "baud": 19200, "model": "IC-705"}
    assert "cat" not in radios.clean([{"name": "x", "device": "y", "gate": "voice", "cat": None}])[0]
    with pytest.raises(ValueError, match="CAT port"):
        radios.clean([{"name": "x", "device": "y", "gate": "voice", "cat": {"port": "rm -rf"}}])
