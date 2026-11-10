"""CAT control of an Icom radio (CI-V): the station reads what the radio is tuned to, and tunes it for its nets.

Owner 2026-11-10: "add a capability to run CAT control to set the frequency automatically" — for VHF/UHF and HF nets
alike. A radio with `cat:` in its settings (Radios… → CAT port, or station.yaml / data/radios.json):

    cat: {port: COM11, civ: A4, baud: 19200}     # IC-705 defaults: CI-V address A4 (hex); any baud on a virtual port

* READ: every POLL_S the station asks the radio its frequency and mode; a change is logged on the radio's card and on
  every later transmission, exactly as if the operator had typed it (Station.set_radio). The README's "frequency read
  from the radio" item.
* TUNE FOR A NET: when a scheduled net on that radio starts (the same edge that resumes a paused radio, LEAD_MIN
  before), the radio is set to the net's frequency and mode, with tone squelch OFF so nothing is muted (a repeater
  net's offset and tone are set too when the net gives them, so the operator can check in). What the radio was on
  before is remembered (data/operator.json, survives a restart) and put back when the net's window ends — unless the
  operator retuned the radio during the net (then the operator is in charge and nothing is put back).
* ANY BAND the radio receives (IC-705: 0.03-199.999 MHz and 400-470 MHz); a net outside it is skipped with a reason.
* NEVER TRANSMITS: only the commands in ALLOWED can be sent (reads; frequency, mode, data mode, duplex, offset, tone).
  PTT (0x1C) and everything else are refused before anything reaches the port; and the port is opened with RTS and
  DTR held low (serial_port), since a radio or interface set to key on those lines would otherwise transmit.

Wire format (Icom CI-V): FE FE <radio> <E0> <cmd> [sub] [data] FD. With "CI-V USB echo back" on, the radio's port
echoes our own frame first; frames from E0 are ours and are skipped. FB = OK, FA = refused.
On Wi-Fi the IC-705's CI-V comes through Icom Remote Utility's virtual COM port (the same program that gives the
station its audio); over USB it is the radio's own first COM port. One program per COM port: a logging program or
Winlink using CAT on the same port blocks this one (the card says so).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

CTRL = 0xE0                     # our (controller) address
POLL_S = 5.0
REPLY_S = 1.0                   # how long to wait for the radio's answer
RETRY_S = 30.0                  # a missing / busy port is tried again this often
QUIET_WAIT_S = 20.0             # a net start waits this long for a transmission in progress to end before retuning

# command (and sub-command) -> what it is; nothing else can be sent
ALLOWED = {(0x03,): "read frequency", (0x04,): "read mode", (0x05,): "set frequency", (0x06,): "set mode",
           (0x0D,): "set duplex offset", (0x0F,): "set duplex", (0x16, 0x42): "repeater tone on/off",
           (0x16, 0x43): "tone squelch on/off", (0x1B, 0x00): "set repeater tone", (0x1A, 0x06): "data mode"}

MODE_CODE = {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW": 0x03, "FM": 0x05}
CODE_MODE = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "DIG", 0x05: "FM", 0x07: "CW", 0x08: "DIG",
             0x17: "DIG"}      # 04 RTTY, 07 CW-R, 08 RTTY-R, 17 D-STAR DV
COVERAGE = {"IC-705": ((0.03, 199.999999), (400.0, 470.0))}


class CatError(RuntimeError):
    pass


def bcd_freq(hz: int) -> bytes:
    """146_720_000 -> 00 00 72 46 01 (five bytes, two digits each, least significant first)."""
    out = []
    for _ in range(5):
        two = hz % 100
        hz //= 100
        out.append((two // 10) << 4 | two % 10)
    return bytes(out)


def freq_bcd(b: bytes) -> int:
    hz, mul = 0, 1
    for x in b[:5]:
        hz += ((x >> 4) * 10 + (x & 0x0F)) * mul
        mul *= 100
    return hz


def bcd_offset(mhz: float) -> bytes:
    """Duplex offset in 100 Hz units, three bytes least significant first: 5 MHz -> 00 00 05, 0.6 MHz -> 00 60 00."""
    n = int(round(abs(mhz) * 10_000))
    return bytes(((n // 10 ** (2 * i + 1)) % 10) << 4 | (n // 10 ** (2 * i)) % 10 for i in range(3))


def bcd_tone(hz: float) -> bytes:
    """CTCSS tone in 0.1 Hz, three bytes most significant first: 88.5 -> 00 08 85, 103.5 -> 00 10 35."""
    s = f"{int(round(hz * 10)):06d}"
    return bytes(int(s[i:i + 2], 16) for i in (0, 2, 4))


def frame(radio: int, cmd: int, sub: int | None = None, data: bytes = b"") -> bytes:
    key = (cmd, sub) if sub is not None else (cmd,)
    if key not in ALLOWED:
        raise CatError(f"CI-V command {key} is not one the station may send")
    return bytes([0xFE, 0xFE, radio, CTRL, cmd] + ([sub] if sub is not None else [])) + data + b"\xFD"


def parse(buf: bytearray) -> list[bytes]:
    """Complete frames (FE FE ... FD) taken off the front of `buf`; garbage before a preamble is dropped."""
    out = []
    while True:
        i = buf.find(b"\xFE\xFE")
        if i < 0:
            del buf[:max(0, len(buf) - 1)]
            return out
        j = buf.find(b"\xFD", i + 2)
        if j < 0:
            del buf[:i]
            return out
        out.append(bytes(buf[i:j + 1]))
        del buf[:j + 1]


def serial_port(port: str, baud: int, serial_mod=None):
    """Open the CI-V port with RTS and DTR held LOW before it opens: an Icom radio (USB SEND / USB Keying set to RTS
    or DTR) or a Digirig keys the transmitter on those lines, and opening a port raises them by default."""
    if serial_mod is None:
        import serial as serial_mod                 # pyserial: in the Windows requirements
    s = serial_mod.Serial()
    s.port, s.baudrate, s.timeout, s.write_timeout = port, baud, 0.1, 1.0
    s.rts, s.dtr = False, False
    s.open()
    return s


class Icom:
    """One radio's CI-V conversation. `transport` has write(bytes) and read(n) -> bytes (pyserial's Serial)."""

    def __init__(self, transport, civ: int = 0xA4, model: str = "IC-705"):
        self.t, self.civ, self.model = transport, civ, model
        self.lock = threading.Lock()
        self.buf = bytearray()

    def close(self):
        try:
            self.t.close()
        except Exception:  # noqa: BLE001
            pass

    def ask(self, cmd: int, sub: int | None = None, data: bytes = b"") -> bytes:
        """Send one command; -> the radio's reply payload after cmd/sub (b"" for FB). Raises CatError."""
        out = frame(self.civ, cmd, sub, data)
        with self.lock:
            self.t.write(out)
            end = time.monotonic() + REPLY_S
            while time.monotonic() < end:
                chunk = self.t.read(64)
                if chunk:
                    self.buf += chunk
                for f in parse(self.buf):
                    if len(f) < 6 or f[2] != CTRL or f[3] != self.civ:
                        continue                      # our own echo, or a frame for someone else
                    body = f[4:-1]
                    if body[:1] == b"\xFB":
                        return b""
                    if body[:1] == b"\xFA":
                        raise CatError(f"the radio refused: {ALLOWED[(cmd, sub) if sub is not None else (cmd,)]}")
                    head = bytes([cmd] + ([sub] if sub is not None else []))
                    if body.startswith(head):
                        return body[len(head):]
            raise CatError("no answer from the radio (is CI-V on, the address right, the port the radio's?)")

    # ----- reads -----
    def frequency(self) -> float:
        return freq_bcd(self.ask(0x03)) / 1e6

    def mode(self) -> str:
        r = self.ask(0x04)
        m = CODE_MODE.get(r[0], "DIG") if r else "FM"
        if m in ("USB", "LSB"):
            try:
                d = self.ask(0x1A, 0x06)
                if d[:1] and d[0]:
                    return "DIG"                       # USB-D / LSB-D: a digital mode on SSB
            except CatError:
                pass
        return m

    # ----- tuning -----
    def covers(self, mhz: float) -> bool:
        return any(a <= mhz <= b for a, b in COVERAGE.get(self.model, ((0.03, 3000.0),)))

    def tune(self, mhz: float, mode: str | None, offset_mhz: float | None = None, tone_hz: float | None = None):
        """Frequency and mode; FM: tone squelch off, and the net's repeater offset/tone when it gives them."""
        if not self.covers(mhz):
            raise CatError(f"{mhz:.4f} MHz is outside what the {self.model} receives")
        self.ask(0x05, data=bcd_freq(int(round(mhz * 1e6))))
        m = (mode or "").upper()
        if m:
            self.ask(0x06, data=bytes([MODE_CODE.get("USB" if m == "DIG" else m, 0x05)]))
            if m in ("USB", "LSB", "DIG"):
                self.ask(0x1A, 0x06, bytes([1 if m == "DIG" else 0, 1 if m == "DIG" else 0]))
        if m == "FM":
            self.ask(0x16, 0x43, b"\x00")              # tone squelch off: hear everything on the frequency
            if offset_mhz is not None:
                self.ask(0x0F, data=bytes([0x10 if not offset_mhz else 0x11 if offset_mhz < 0 else 0x12]))
                if offset_mhz:
                    self.ask(0x0D, data=bcd_offset(offset_mhz))
            if tone_hz:
                self.ask(0x1B, 0x00, bcd_tone(tone_hz))
                self.ask(0x16, 0x42, b"\x01")
            elif offset_mhz is not None:
                self.ask(0x16, 0x42, b"\x00")


def parse_cat(c) -> dict | None:
    """station.yaml / radios.json `cat:` -> {"port", "civ" (int), "baud", "model"}; "COM11" alone is accepted."""
    if not c:
        return None
    if isinstance(c, str):
        c = {"port": c}
    port = str(c.get("port") or "").strip()
    if not port:
        return None
    civ = c.get("civ", 0xA4)
    civ = int(civ, 16) if isinstance(civ, str) else int(civ)
    return {"port": port, "civ": civ, "baud": int(c.get("baud") or 19200), "model": str(c.get("model") or "IC-705")}


class CatControl:
    """The station's CAT radios: polling, tuning for nets, putting the radio back after."""

    def __init__(self, station, factory=None):
        self.st = station
        self.factory = factory or (lambda cfg: Icom(serial_port(cfg["port"], cfg["baud"]), cfg["civ"], cfg["model"]))
        self.cfg = {}
        for r in station.cfg["radios"]:
            c = parse_cat(r.get("cat"))
            if c:
                self.cfg[r["id"]] = c
        self.rigs: dict[str, Icom | None] = {}
        self.next_try: dict[str, float] = {}
        self.last: dict[str, tuple[float, str]] = {}
        self.lock = threading.Lock()
        for rid, c in self.cfg.items():
            if rid in station.radios:
                station.radios[rid]["cat"] = {"port": c["port"], "ok": None, "error": None, "tuned_for": None}

    def has(self, rid) -> bool:
        return rid in self.cfg

    def _rig(self, rid) -> Icom | None:
        with self.lock:
            rig = self.rigs.get(rid)
            if rig is not None:
                return rig
            if time.monotonic() < self.next_try.get(rid, 0):
                return None
            try:
                rig = self.rigs[rid] = self.factory(self.cfg[rid])
                return rig
            except Exception as e:  # noqa: BLE001 - no port, busy port: the card says why
                self.next_try[rid] = time.monotonic() + RETRY_S
                self._status(rid, False, f"can't open {self.cfg[rid]['port']}: {e}")
                return None

    def _drop(self, rid, why):
        with self.lock:
            rig = self.rigs.pop(rid, None)
            self.next_try[rid] = time.monotonic() + RETRY_S
        if rig:
            rig.close()
        self._status(rid, False, why)

    def _status(self, rid, ok, error=None, **kw):
        r = self.st.radios.get(rid)
        if not r:
            return
        c = r.setdefault("cat", {"port": self.cfg[rid]["port"]})
        changed = c.get("ok") != ok or c.get("error") != error or any(c.get(k) != v for k, v in kw.items())
        c.update(ok=ok, error=error, **kw)
        if changed and error:
            print(f"{datetime.now():%H:%M:%S} {rid} CAT: {error}", flush=True)
        if changed:
            self.st.bus.publish("radios", {"radios": list(self.st.radios.values()), "net": self.st.schedule_now()})

    # ----- reading the radio -----
    def poll(self, rid):
        rig = self._rig(rid)
        if rig is None:
            return None
        try:
            f, m = rig.frequency(), rig.mode()
        except Exception as e:  # noqa: BLE001
            self._drop(rid, str(e))
            return None
        self.last[rid] = (f, m)
        self._status(rid, True)
        r = self.st.radios[rid]
        if abs((r.get("freq_mhz") or 0) - f) > 0.0005 or m != r.get("mode"):
            self.st.set_radio(rid, round(f, 4), m)
        return f, m

    def run(self, stop: threading.Event):
        while not stop.wait(POLL_S):
            for rid in self.cfg:
                try:
                    self.poll(rid)
                except Exception as e:  # noqa: BLE001 - CAT never stops the station
                    print(f"{rid} CAT poll: {type(e).__name__}: {e}", flush=True)

    def tune_now(self, rid, freq_mhz, mode=None) -> dict:
        """The card's Change on a CAT radio: tune the radio (otherwise the next poll would put the card back to what
        the radio is on). Raises ValueError with words for the operator."""
        try:
            f = round(float(str(freq_mhz).replace(",", ".")), 6)
        except (TypeError, ValueError):
            raise ValueError("frequency must be a number in MHz, like 146.940") from None
        rig = self._rig(rid)
        if rig is None:
            raise ValueError(f"CAT: {(self.st.radios[rid].get('cat') or {}).get('error') or 'the radio is not answering'}")
        if not rig.covers(f):
            raise ValueError(f"{f:.4f} MHz is outside what the {rig.model} receives")
        try:
            rig.tune(f, mode)
        except Exception as e:  # noqa: BLE001
            self._drop(rid, str(e))
            raise ValueError(f"CAT: {e}") from None
        self._save_restore(rid, None)            # the operator tuned it: nothing to put back after a net
        self._status(rid, True, None, tuned_for=None)
        return self.st.set_radio(rid, round(f, 4), mode)

    # ----- nets -----
    def _saved_restore(self, rid):
        return ((self.st._saved().get("radios") or {}).get(rid) or {}).get("cat_restore")

    def _save_restore(self, rid, value):
        with self.st.lock:
            saved = self.st._saved()
            s = saved.setdefault("radios", {}).setdefault(rid, {})
            if value is None:
                s.pop("cat_restore", None)
            else:
                s["cat_restore"] = value
            self.st._save(saved)

    def on_net(self, rid, net: dict | None, wait=True):
        """The schedule's edge for this radio: a net's window opened (`net`) or closed (None). In a thread."""
        if rid not in self.cfg:
            return
        t = threading.Thread(target=self._on_net, args=(rid, net, wait), daemon=True, name=f"cat-{rid}")
        t.start()
        return t

    def _on_net(self, rid, net, wait):
        try:
            if net and net.get("freq_mhz"):
                self._tune_for(rid, net, wait)
            elif not net:
                self._put_back(rid)
        except Exception as e:  # noqa: BLE001
            self._status(rid, False, f"{type(e).__name__}: {e}")

    def _quiet(self, rid, wait):
        end = time.monotonic() + (QUIET_WAIT_S if wait else 0)
        while self.st.radios.get(rid, {}).get("live_tx") and time.monotonic() < end:
            time.sleep(0.5)

    def _tune_for(self, rid, net, wait):
        rig = self._rig(rid)
        if rig is None:
            return
        f, mode = float(net["freq_mhz"]), net.get("mode")
        if not rig.covers(f):
            return self._status(rid, True, f"{net['name']}: {f:.4f} MHz is outside what the radio receives; not tuned")
        try:
            before = rig.frequency(), rig.mode()
        except Exception as e:  # noqa: BLE001
            return self._drop(rid, str(e))
        if self._saved_restore(rid) is None and abs(before[0] - f) > 0.0005:
            self._save_restore(rid, {"freq_mhz": round(before[0], 6), "mode": before[1], "net_freq": f})
        elif self._saved_restore(rid) is not None:
            r = self._saved_restore(rid)
            r["net_freq"] = f                       # back-to-back nets: still put back what came before the first
            self._save_restore(rid, r)
        self._quiet(rid, wait)
        try:
            rig.tune(f, mode, net.get("offset_mhz"), net.get("tone_hz"))
        except Exception as e:  # noqa: BLE001
            return self._drop(rid, f"tuning for {net['name']}: {e}")
        self._status(rid, True, None, tuned_for=net["name"])
        self.st.set_radio(rid, round(f, 4), mode or self.st.radios[rid].get("mode"))
        print(f"{datetime.now():%H:%M:%S} {rid} CAT: tuned to {f:.4f} MHz {mode or ''} for {net['name']}", flush=True)

    def _put_back(self, rid):
        back = self._saved_restore(rid)
        if not back:
            if (self.st.radios.get(rid, {}).get("cat") or {}).get("tuned_for"):
                self._status(rid, True, None, tuned_for=None)
            return
        rig = self._rig(rid)
        if rig is None:
            return                                  # kept: tried again at the next edge or restart
        try:
            now = rig.frequency()
        except Exception as e:  # noqa: BLE001
            return self._drop(rid, str(e))
        self._save_restore(rid, None)
        if abs(now - float(back.get("net_freq") or now)) > 0.0005:
            print(f"{datetime.now():%H:%M:%S} {rid} CAT: retuned by the operator during the net; left as it is",
                  flush=True)
            return self._status(rid, True, None, tuned_for=None)
        self._quiet(rid, True)
        try:
            rig.tune(float(back["freq_mhz"]), back.get("mode"))
        except Exception as e:  # noqa: BLE001
            return self._drop(rid, f"putting the radio back: {e}")
        self._status(rid, True, None, tuned_for=None)
        self.st.set_radio(rid, round(float(back["freq_mhz"]), 4), back.get("mode"))
        print(f"{datetime.now():%H:%M:%S} {rid} CAT: put back on {float(back['freq_mhz']):.4f} MHz "
              f"{back.get('mode') or ''} after the net", flush=True)
