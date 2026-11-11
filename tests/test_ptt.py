"""Transmit (PTT) setup per radio: off by default, refused while off, always let go."""
import time

import pytest

from station import ptt, radios


def _radio(**tx):
    return {"name": "Net radio", "device": "Digirig Lite", "gate": "voice", "mode": "FM", "tx": tx or None}


def test_listen_only_is_the_default():
    r = radios.clean([_radio()])[0]
    assert "tx" not in r
    r = radios.clean([_radio(ptt="none", enabled=True)])[0]
    assert "tx" not in r


def test_ra_board_setup_is_kept_and_switched_off_unless_exactly_true():
    r = radios.clean([_radio(ptt="cm108", hid="\\\\?\\hid#vid_0d8c", out="Digirig Lite", enabled="yes")])[0]
    assert r["tx"] == {"enabled": False, "ptt": "cm108", "out": "Digirig Lite", "max_s": 60,
                       "hid": "\\\\?\\hid#vid_0d8c", "pin": 3}


@pytest.mark.parametrize("tx,msg", [
    ({"ptt": "cm108"}, "choose the interface"),
    ({"ptt": "cm108", "hid": "p", "pin": 9}, "pin must be 1-8"),
    ({"ptt": "rts", "port": "nope"}, "like COM7"),
    ({"ptt": "cm108", "hid": "p", "max_s": 600}, "5-180"),
    ({"ptt": "cm108", "hid": "p", "enabled": True}, "sound output"),
    ({"ptt": "bogus"}, "PTT must be"),
])
def test_bad_setups_are_refused_in_words(tx, msg):
    with pytest.raises(ValueError, match=msg):
        radios.clean([_radio(**tx)])


def test_ptt_never_shares_the_cat_port():
    r = _radio(ptt="rts", port="com11") | {"cat": {"port": "COM11"}}
    with pytest.raises(ValueError, match="CAT port"):
        radios.clean([r])


def test_cm108_report_matches_direwolf():
    assert ptt.cm108_report(3, True) == bytes([0, 0, 4, 4, 0])
    assert ptt.cm108_report(3, False) == bytes([0, 0, 4, 0, 0])


class FakeDev:
    def __init__(self):
        self.states, self.closed = [], False

    def set(self, on):
        self.states.append(on)

    def close(self):
        self.set(False)
        self.closed = True


class St:
    def __init__(self, tx):
        self.radio_cfg = {"net": {"id": "net", "tx": tx}}


def _tx(**k):
    return {"ptt": "cm108", "hid": "p", "out": "Digirig Lite", "max_s": 5} | k


def test_switched_off_refuses_before_opening_anything():
    opened = []
    t = ptt.Transmitter(St(_tx(enabled=False)), factory=lambda c: opened.append(c) or FakeDev())
    with pytest.raises(ptt.PttRefused, match="switched off"):
        with t.key("net"):
            pass
    assert opened == []
    with pytest.raises(ptt.PttRefused, match="no transmit"):
        with ptt.Transmitter(St(None)).key("net"):
            pass


def test_switched_on_keys_and_always_lets_go():
    dev = FakeDev()
    t = ptt.Transmitter(St(_tx(enabled=True)), factory=lambda c: dev)
    with pytest.raises(RuntimeError):
        with t.key("net") as limit:
            assert limit == 5 and t.view()["net"]["on_air"]
            raise RuntimeError("audio failed")
    assert dev.states == [True, False] and dev.closed and not t.on_air


def test_the_watchdog_lets_go_at_the_limit():
    dev = FakeDev()
    t = ptt.Transmitter(St(_tx(enabled=True)), factory=lambda c: dev)
    with t.key("net", seconds=0.1):
        time.sleep(0.4)
        assert dev.states == [True, False]          # let go inside the block


def test_identical_interfaces_are_told_apart_by_their_sound_card():
    assert ptt.usb_token(r"USB\VID_0D8C&PID_0012&MI_03\7&1760c262&0&0003") == "7&1760C262"
    assert ptt.usb_token(r"{1}.USB\VID_0D8C&PID_0012&MI_00\7&1760C262&0&0000") == "7&1760C262"
    assert ptt.usb_token(r"{1}.BTHENUM\{0000111F}\6&1B&0&F4") == ""

    class Hid:
        @staticmethod
        def enumerate():
            return [{"path": b"A", "vendor_id": 0x0D8C, "product_id": 0x12, "product_string": "USB Audio Device"},
                    {"path": b"B", "vendor_id": 0x0D8C, "product_id": 0x12, "product_string": "USB Audio Device"},
                    {"path": b"K", "vendor_id": 0x046D, "product_string": "Keyboard"}]
    parents = {"A": r"USB\VID_0D8C&PID_0012&MI_03\7&AAA&0&0003", "B": r"USB\VID_0D8C&PID_0012&MI_03\7&BBB&0&0003"}
    got = ptt.hid_devices(Hid, sounds={"7&BBB": "Digirig Lite VHF UHF 2 NET-REC"}, parent=parents.get)
    assert [(d["path"], d["sound"]) for d in got] == [("B", "Digirig Lite VHF UHF 2 NET-REC"), ("A", "")]
    assert got[0]["desc"].startswith("Digirig Lite VHF UHF 2 NET-REC — ")
