"""Keep the PC awake and at full speed while the station is transcribing, and only then.

Measured on a 2026 laptop (Intel Core Ultra, Modern Standby, OLED; 2026-11-04): with the screen off, or a
screensaver showing, the firmware holds the processor at about a fifth of its speed (a Python loop: 0.85 s with
the screen on and nobody at the PC, 4.2 s once it went dark), and Task Scheduler queues the operator's tasks.
Windows' own "sleep: never" does not prevent it; only a lit screen does. So while a radio is listening (not
paused: with "listen only during nets", that is during nets) or the accurate pass has work, the station holds a
Windows power request for the display and the system, the same request a video player makes, and drops it
otherwise so the PC can idle (and an OLED screen can rest) between nets.

While it holds, the station also switches Windows to the "HamNet Station" power plan the installer makes (full
speed, no sleep, lid does nothing, USB / Wi-Fi / PCIe power saving off, Intel graphics at maximum) and the power mode
to Best performance, after saving the operator's own plan and mode to data/power_snapshot.json; it puts them back
when it lets go, or at its next start if it stopped while holding. The operator's plan can then favour the battery.

station.yaml `keep_awake`: "listening" (default: as above), "always" (whenever the station runs), "off".
"""
import json
import sys
import uuid
from pathlib import Path

HAMNET_PLAN = "6f1c2d4e-8a3b-4c5d-9e7f-48616d4e6574"   # the installer's "HamNet Station" plan
BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"                 # Windows power mode overlay

MODES = ("listening", "always", "off")


def wanted(mode: str, radios: list[dict], accurate_jobs: list[dict]) -> tuple[bool, str]:
    """(hold the power request?, why). Pure: the station's ticker and the tests call it."""
    if mode == "off":
        return False, "keep_awake: off"
    if mode == "always":
        return True, "keep_awake: always"
    live = [r.get("name") or r.get("id") or "?" for r in radios if not r.get("paused")]
    if live:
        return True, "listening: " + ", ".join(sorted(map(str, live)))
    if any(j.get("state") in ("queued", "running") for j in accurate_jobs):
        return True, "accurate pass running"
    return False, "no radio listening"


class PowerPlan:
    """The active Windows power plan and power mode (overlay): read, set, saved to a file and put back."""

    def __init__(self, snapshot: Path):
        import ctypes
        self.ct = ctypes
        self.pp = ctypes.WinDLL("powrprof")
        self.snapshot = Path(snapshot)

    def _guid(self, s: str):
        b = uuid.UUID(s).bytes_le
        return (self.ct.c_ubyte * 16).from_buffer_copy(b)

    def active(self) -> str:
        p = self.ct.c_void_p()
        if self.pp.PowerGetActiveScheme(None, self.ct.byref(p)) != 0:
            raise OSError("PowerGetActiveScheme failed")
        g = str(uuid.UUID(bytes_le=self.ct.string_at(p, 16)))
        self.ct.windll.kernel32.LocalFree(p)
        return g

    def overlay(self) -> str | None:
        g = (self.ct.c_ubyte * 16)()
        try:
            if self.pp.PowerGetEffectiveOverlayScheme(self.ct.byref(g)) != 0:
                return None
        except AttributeError:                                   # Windows 10 before 1709: no power modes
            return None
        return str(uuid.UUID(bytes_le=bytes(g)))

    def exists(self, guid: str) -> bool:
        p = self.ct.c_void_p()
        size = self.ct.c_ulong(0)
        r = self.pp.PowerReadFriendlyName(None, self.ct.byref(self._guid(guid)), None, None, None, self.ct.byref(size))
        return r == 0 and size.value > 0

    def set(self, plan: str, overlay: str | None):
        if self.pp.PowerSetActiveScheme(None, self.ct.byref(self._guid(plan))) != 0:
            raise OSError(f"PowerSetActiveScheme {plan} failed")
        if overlay:
            try:
                self.pp.PowerSetActiveOverlayScheme(self.ct.byref(self._guid(overlay)))
            except AttributeError:
                pass

    def on_battery(self) -> bool:
        class SPS(self.ct.Structure):
            _fields_ = [("ACLineStatus", self.ct.c_ubyte), ("BatteryFlag", self.ct.c_ubyte),
                        ("BatteryLifePercent", self.ct.c_ubyte), ("SystemStatusFlag", self.ct.c_ubyte),
                        ("BatteryLifeTime", self.ct.c_ulong), ("BatteryFullLifeTime", self.ct.c_ulong)]
        st = SPS()
        self.ct.windll.kernel32.GetSystemPowerStatus(self.ct.byref(st))
        return st.ACLineStatus == 0

    def own_mode(self) -> str | None:
        """The operator's own power mode for the current power source (Settings → Power mode). Windows keeps the
        plugged-in and battery choices in the registry; a program setting the mode changes only the live one, and
        Windows re-applies the saved choice when the power source changes (measured 2026-11-04)."""
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes")
            return winreg.QueryValueEx(k, "ActiveOverlayDcPowerScheme" if self.on_battery()
                                       else "ActiveOverlayAcPowerScheme")[0]
        except OSError:
            return None

    def refresh(self):
        """While holding: Windows switches the mode itself when the power source changes; put full speed back."""
        if self.active() != HAMNET_PLAN or self.overlay() != BEST_PERFORMANCE:
            self.set(HAMNET_PLAN, BEST_PERFORMANCE)

    def switch(self) -> bool:
        """Save the operator's plan + mode and switch to the HamNet plan. False when the plan is not installed."""
        if not self.exists(HAMNET_PLAN):
            return False
        if not self.snapshot.exists():                           # never overwrite a saved plan with our own
            cur = self.active()
            if cur != HAMNET_PLAN:
                self.snapshot.write_text(json.dumps({"plan": cur, "overlay": self.overlay()}))
        self.set(HAMNET_PLAN, BEST_PERFORMANCE)
        return True

    def restore(self) -> dict | None:
        """Put back the saved plan + mode (and forget them). None when nothing was saved."""
        if not self.snapshot.exists():
            return None
        snap = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.set(snap["plan"], self.own_mode() or snap.get("overlay"))   # the operator's choice for the power source now
        self.snapshot.unlink()
        return snap


class KeepAwake:
    """One Windows power request (display + system required) and the HamNet power plan, set and cleared on change.
    A no-op elsewhere."""

    def __init__(self, reason: str = "HamNet Station is transcribing", snapshot: Path | None = None):
        self.held = False
        self.handle = None
        self.ok = sys.platform == "win32"
        self.reason = reason
        self.plan = None
        if self.ok and snapshot is not None:
            try:
                self.plan = PowerPlan(snapshot)
                back = self.plan.restore()                       # a previous run stopped while holding
                if back:
                    print(f"keep awake: power plan put back after a restart ({back['plan']})", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"keep awake: power plan: {type(e).__name__}: {e}", flush=True)

    def _create(self):
        import ctypes
        from ctypes import wintypes

        class REASON_CONTEXT(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("Flags", wintypes.DWORD), ("SimpleReasonString", wintypes.LPWSTR)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.PowerCreateRequest.restype = wintypes.HANDLE
        k.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
        k.PowerSetRequest.argtypes = k.PowerClearRequest.argtypes = [wintypes.HANDLE, ctypes.c_int]
        self._ctx = REASON_CONTEXT(0, 0x1, self.reason)       # POWER_REQUEST_CONTEXT_SIMPLE_STRING
        h = k.PowerCreateRequest(ctypes.byref(self._ctx))
        if not h or h == wintypes.HANDLE(-1).value:
            raise OSError(f"PowerCreateRequest failed ({ctypes.get_last_error()})")
        self.k, self.handle = k, h

    def set(self, want: bool) -> bool:
        """Hold or release. Returns True when the state changed."""
        if self.ok and want and self.held and self.plan is not None:
            try:
                self.plan.refresh()
            except Exception as e:  # noqa: BLE001
                print(f"keep awake: power plan: {type(e).__name__}: {e}", flush=True)
        if not self.ok or want == self.held:
            return False
        try:
            if self.handle is None:
                self._create()
            for kind in (0, 1):                                  # PowerRequestDisplayRequired, SystemRequired
                (self.k.PowerSetRequest if want else self.k.PowerClearRequest)(self.handle, kind)
            if want:                                             # light a screen that is already off
                self.k.SetThreadExecutionState(0x2 | 0x1)        # ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED, once
            if self.plan is not None:
                try:
                    if want:
                        if not self.plan.switch():
                            print("keep awake: no HamNet Station power plan (re-run the installer)", flush=True)
                    else:
                        self.plan.restore()
                except Exception as e:  # noqa: BLE001 — the power request still holds
                    print(f"keep awake: power plan: {type(e).__name__}: {e}", flush=True)
        except Exception as e:  # noqa: BLE001 — never stop the station over power management
            print(f"keep awake: {type(e).__name__}: {e}", flush=True)
            self.ok = False
            return False
        self.held = want
        return True
