"""Keying a radio's transmitter (PTT) — configured per radio, OFF until the operator turns it on.

Owner 2026-11-11: "We have been working up listen only, but are preparing to allow the Sidekick AI agent to key up and
transmit. Let's get it configured on the radio config so that we can do it when ready. It should have a switch that
is off by default for now."

A radio's `tx:` (Radios… → Transmit):

    tx: {enabled: false, ptt: cm108, hid: "<HID path>", pin: 3, out: "Digirig Lite NET-REC", max_s: 60}

* ptt "cm108" — the sound card's own GPIO pin, what VARA FM calls "RA-Board" (Direwolf: "PTT CM108"): Digirig Lite,
  AIOC, RA-35/RA-Board, most CM108/CM119 USB interfaces. Pin 3 is the usual one (Digirig Lite, AIOC).
* ptt "rts" / "dtr" — a serial line (Digirig Mobile, SignaLink-style cables): its own COM port, never the CAT port
  (station/cat.py holds that one with RTS and DTR LOW so it can never key).
* `out` — the sound OUTPUT the transmitted audio goes to (the interface's speaker side).
* `max_s` — the transmitter is let go after this long whatever happens (a watchdog, 5-180 s).
* `enabled` — the switch. False (the default, and anything that is not exactly True) refuses every key-up before any
  device is opened. Nothing in the station keys a radio yet: Sidekick still only drafts. When it is allowed to send,
  it goes through Transmitter.key(), and only on a radio whose switch is on.

Part 97: a voice transmission like Sidekick's needs a control operator (97.109: automatic control is for repeaters,
beacons and some data), and the station's ID (97.119) — the code that sends will have to carry both.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager

PTTS = ("none", "cm108", "rts", "dtr")
DEFAULT_PIN = 3
MAX_S = (5, 180)
DEFAULT_MAX_S = 60
CMEDIA = 0x0D8C                                  # C-Media: CM108/CM119 (Digirig Lite, AIOC, RA-Board)


class PttRefused(RuntimeError):
    """A key-up refused before anything was opened (switch off, not configured)."""


def clean_tx(tx, name: str, cat_port: str | None = None) -> dict | None:
    """Radios… → a radio's `tx` as saved. None when it has no transmit setup. Raises ValueError for a bad one."""
    if not tx or not isinstance(tx, dict):
        return None
    ptt = str(tx.get("ptt") or "none").lower()
    if ptt not in PTTS:
        raise ValueError(f"{name}: PTT must be one of {', '.join(PTTS)}")
    if ptt == "none":
        return None
    try:
        max_s = int(tx.get("max_s") or DEFAULT_MAX_S)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: the transmit time limit must be whole seconds") from None
    if not MAX_S[0] <= max_s <= MAX_S[1]:
        raise ValueError(f"{name}: the transmit time limit must be {MAX_S[0]}-{MAX_S[1]} seconds")
    out = {"enabled": tx.get("enabled") is True, "ptt": ptt, "out": str(tx.get("out") or "").strip(), "max_s": max_s}
    if ptt == "cm108":
        hid = str(tx.get("hid") or "").strip()
        if not hid:
            raise ValueError(f"{name}: choose the interface (USB HID) whose pin keys the radio")
        try:
            pin = int(tx.get("pin") or DEFAULT_PIN)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: the GPIO pin must be 1-8") from None
        if not 1 <= pin <= 8:
            raise ValueError(f"{name}: the GPIO pin must be 1-8")
        out |= {"hid": hid, "pin": pin}
    else:
        port = str(tx.get("port") or "").strip()
        if not re.fullmatch(r"(COM\d{1,3}|/dev/[\w./-]+)", port, re.I):
            raise ValueError(f"{name}: the PTT port must be like COM7")
        port = port.upper() if port.upper().startswith("COM") else port
        if cat_port and port.upper() == str(cat_port).upper():
            raise ValueError(f"{name}: PTT on the CAT port is not supported (the CAT code holds RTS/DTR low)")
        out["port"] = port
    if out["enabled"] and not out["out"]:
        raise ValueError(f"{name}: choose the sound output the transmitted audio goes to before turning transmit on")
    return out


def hid_devices(hid_mod=None) -> list[dict]:
    """USB HID interfaces that can key a radio (C-Media first): [{path, desc}]. Empty without hidapi."""
    try:
        if hid_mod is None:
            import hid as hid_mod                    # hidapi: in the Windows requirements
        found = list(hid_mod.enumerate())
    except Exception:  # noqa: BLE001 - no hidapi (a dev machine)
        return []
    out, seen = [], set()
    for d in found:
        path = d.get("path") or b""
        path = path.decode("utf-8", "replace") if isinstance(path, bytes) else str(path)
        vid = d.get("vendor_id") or 0
        name = " ".join(x for x in (d.get("manufacturer_string"), d.get("product_string")) if x) or "HID device"
        if path in seen or not (vid == CMEDIA or re.search(r"audio|digirig|aioc|cm1\d\d|ra-?board", name, re.I)):
            continue
        seen.add(path)
        out.append({"path": path, "desc": f"{name} ({vid:04X}:{d.get('product_id') or 0:04X})", "cmedia": vid == CMEDIA})
    return sorted(out, key=lambda x: (not x["cmedia"], x["desc"]))


def outputs(devices: list[dict] | None = None, hostapis: list[dict] | None = None) -> list[str]:
    """The sound OUTPUTS (where transmitted audio goes), by base name."""
    from .radios import base_name
    if devices is None:
        try:
            import sounddevice as sd
            devices, hostapis = list(sd.query_devices()), list(sd.query_hostapis())
        except Exception:  # noqa: BLE001
            return []
    seen = set()
    for d in devices:
        if d.get("max_output_channels", 0) <= 0:
            continue
        api = (hostapis or [{}] * 99)[d.get("hostapi", 0)].get("name", "") if hostapis else ""
        if api == "Windows WDM-KS":
            continue
        n = base_name(d["name"])
        if n and not n.lower().startswith(("microsoft sound mapper", "primary sound driver")):
            seen.add(n)
    names = sorted(seen)
    return [n for n in names if not any(o != n and o.startswith(n) and len(n) >= 31 for o in names)]


def cm108_report(pin: int, on: bool) -> bytes:
    """The HID output report that sets one CM108 GPIO (as Direwolf's cm108.c): report id 0, 0, direction mask, state
    mask, 0."""
    mask = 1 << (pin - 1)
    return bytes([0, 0, mask, mask if on else 0, 0])


class CM108:
    def __init__(self, path: str, pin: int, hid_mod=None):
        if hid_mod is None:
            import hid as hid_mod
        self.dev = hid_mod.device()
        self.dev.open_path(path.encode() if isinstance(path, str) else path)
        self.pin = pin

    def set(self, on: bool):
        if self.dev.write(cm108_report(self.pin, on)) < 0:
            raise OSError("the interface refused the PTT write")

    def close(self):
        try:
            self.set(False)
        finally:
            self.dev.close()


class SerialLine:
    def __init__(self, port: str, line: str, serial_mod=None):
        if serial_mod is None:
            import serial as serial_mod
        s = serial_mod.Serial()
        s.port, s.baudrate = port, 9600
        s.rts, s.dtr = False, False                     # held low before opening: opening raises them by default
        s.open()
        self.s, self.line = s, line

    def set(self, on: bool):
        setattr(self.s, self.line, bool(on))

    def close(self):
        try:
            self.set(False)
        finally:
            self.s.close()


class Transmitter:
    """One per station. key(rid) is the only way to key a radio; it refuses while the radio's switch is off."""

    def __init__(self, station, factory=None, clock=time.monotonic):
        self.st = station
        self.factory = factory or self._open
        self.clock = clock
        self.lock = threading.Lock()
        self.on_air: dict[str, float] = {}

    def cfg(self, rid: str) -> dict | None:
        """The radio's checked `tx` (station.yaml's is not checked by Radios…, so it is checked here); a bad one is
        none at all."""
        r = getattr(self.st, "radio_cfg", {}).get(rid) or {}
        try:
            return clean_tx(r.get("tx"), rid, (r.get("cat") or {}).get("port") if isinstance(r.get("cat"), dict) else r.get("cat"))
        except ValueError:
            return None

    def view(self) -> dict:
        out = {}
        for rid in (getattr(self.st, "radio_cfg", {}) or {}):
            t = self.cfg(rid)
            if t:
                out[rid] = {"ptt": t["ptt"], "enabled": t.get("enabled") is True, "on_air": rid in self.on_air,
                            "max_s": t["max_s"]}
        return out

    @staticmethod
    def _open(t: dict):
        if t["ptt"] == "cm108":
            return CM108(t["hid"], t["pin"])
        return SerialLine(t["port"], t["ptt"])

    def allowed(self, rid: str) -> tuple[bool, str]:
        t = self.cfg(rid)
        if not t:
            return False, "this radio has no transmit (PTT) setup"
        if t.get("enabled") is not True:
            return False, "transmit is switched off for this radio (Radios… → Transmit)"
        return True, ""

    @contextmanager
    def key(self, rid: str, seconds: float | None = None):
        """Key the radio for the block; let go at its end, on any error, or at max_s (whichever comes first)."""
        ok, why = self.allowed(rid)
        if not ok:
            raise PttRefused(why)
        t = self.cfg(rid)
        limit = min(float(seconds or t["max_s"]), float(t["max_s"]))
        with self.lock:
            if rid in self.on_air:
                raise PttRefused("this radio is already transmitting")
            dev = self.factory(t)
            self.on_air[rid] = self.clock()
        done = threading.Event()

        def watchdog():
            if not done.wait(limit):
                try:
                    dev.set(False)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=watchdog, daemon=True, name=f"ptt-{rid}").start()
        try:
            dev.set(True)
            yield limit
        finally:
            done.set()
            try:
                dev.close()                                # close() lets go first
            finally:
                with self.lock:
                    self.on_air.pop(rid, None)
