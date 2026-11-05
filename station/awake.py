"""Keep the PC awake and at full speed while the station is transcribing, and only then.

Measured on a 2026 laptop (Intel Core Ultra, Modern Standby, OLED; 2026-11-04): with the screen off, or a
screensaver showing, the firmware holds the processor at about a fifth of its speed (a Python loop: 0.85 s with
the screen on and nobody at the PC, 4.2 s once it went dark), and Task Scheduler queues the operator's tasks.
Windows' own "sleep: never" does not prevent it; only a lit screen does. So while a radio is listening (not
paused: with "listen only during nets", that is during nets) or the accurate pass has work, the station holds a
Windows power request for the display and the system, the same request a video player makes, and drops it
otherwise so the PC can idle (and an OLED screen can rest) between nets.

station.yaml `keep_awake`: "listening" (default: as above), "always" (whenever the station runs), "off".
"""
import sys

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


class KeepAwake:
    """One Windows power request (display + system required), set and cleared on change. A no-op elsewhere."""

    def __init__(self, reason: str = "HamNet Station is transcribing"):
        self.held = False
        self.handle = None
        self.ok = sys.platform == "win32"
        self.reason = reason

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
        if not self.ok or want == self.held:
            return False
        try:
            if self.handle is None:
                self._create()
            for kind in (0, 1):                                  # PowerRequestDisplayRequired, SystemRequired
                (self.k.PowerSetRequest if want else self.k.PowerClearRequest)(self.handle, kind)
            if want:                                             # light a screen that is already off
                self.k.SetThreadExecutionState(0x2 | 0x1)        # ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED, once
        except Exception as e:  # noqa: BLE001 — never stop the station over power management
            print(f"keep awake: {type(e).__name__}: {e}", flush=True)
            self.ok = False
            return False
        self.held = want
        return True
