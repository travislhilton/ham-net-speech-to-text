"""Safeguards for benchmarks run on a live station PC (tools/accbench/run.py uses them on Windows by default).

A laptop station crashed three times on 2026-11-06 (bugcheck 0x10E VIDEO_MEMORY_MANAGEMENT_INTERNAL, the Intel graphics
driver's memory manager): the first while two large-v3 decodes, the station's accurate pass and its live engines all
shared the integrated GPU. A benchmark must never be the thing that takes the station down, so:

  - one benchmark at a time per PC (a lock file; a second one refuses to start);
  - it runs at below-normal priority;
  - before every ~30 s window (transcribe/decoders.py `pause`) it waits while the station is busy: a transmission is
    being received or transcribed, or the station's own accurate pass is running (GET /api/state);
  - it starts only with `start_free_mb` (default 6000 MB + the model's need) free, and waits at a window while free
    memory is under `wait_free_mb` (4000);
  - a watchdog checks memory every second and ENDS the benchmark at once (TerminateProcess) when free memory or
    commit headroom falls under `kill_free_mb` (2500) — losing a benchmark is cheap, losing the PC is not.

Measured numbers stay honest: waiting time is not counted as decode time (Guard.waited_s is reported).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

STATION = "http://127.0.0.1:8091"


def free_mb() -> tuple[int | None, int | None]:
    """(free physical MB, free commit MB) on Windows; (None, None) elsewhere."""
    if os.name != "nt":
        return None, None
    import ctypes

    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS(); m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return int(m.ullAvailPhys // 2**20), int(m.ullAvailPageFile // 2**20)


def station_busy(url: str = STATION) -> str | None:
    """Why the station needs the PC now, or None (also None when no station answers)."""
    try:
        with urllib.request.urlopen(url + "/api/state", timeout=2) as r:
            s = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None
    if any(r.get("carrier") for r in s.get("radios", [])):
        return "receiving"
    if any(t.get("status") in ("live", "transcribing") for t in s.get("tx", [])[-20:]):
        return "transcribing"
    if any(j.get("state") == "running" for j in (s.get("accurate") or [])):
        return "its accurate pass is running"
    return None


class Guard:
    def __init__(self, need_mb: int = 7000, start_free_mb: int = 6000, wait_free_mb: int = 4000,
                 kill_free_mb: int = 2500, lock_dir: Path | None = None, url: str = STATION,
                 free=free_mb, busy=station_busy, sleep=time.sleep, die=None, log=print):
        self.need, self.start_free, self.wait_free, self.kill_free = need_mb, start_free_mb, wait_free_mb, kill_free_mb
        self.url, self.free, self.busy, self.sleep, self.log = url, free, busy, sleep, log
        self.die = die or self._terminate
        self.lock = Path(lock_dir or (Path(os.environ.get("TEMP", "/tmp")))) / "accbench.lock"
        self.waited_s = 0.0
        self._stop = threading.Event()

    # ----- start / stop -----
    def start(self, watchdog: bool = True):
        if self.lock.exists():
            try:
                pid = int(self.lock.read_text().strip() or 0)
            except ValueError:
                pid = 0
            if pid and _alive(pid):
                raise SystemExit(f"accbench guard: another benchmark is running (pid {pid}); one at a time")
        self.lock.write_text(str(os.getpid()))
        _below_normal()
        phys, _ = self.free()
        while phys is not None and phys < self.need + self.start_free:
            self.log(f"accbench guard: {phys} MB free, waiting for {self.need + self.start_free} MB before loading")
            self._wait(30)
            phys, _ = self.free()
        if watchdog:
            threading.Thread(target=self._watch, daemon=True).start()
        return self

    def close(self):
        self._stop.set()
        try:
            if self.lock.read_text().strip() == str(os.getpid()):
                self.lock.unlink()
        except OSError:
            pass

    # ----- between windows -----
    def pause(self):
        """Called before every ~30 s window: wait while the station needs the PC or memory is short."""
        while True:
            why = self.busy(self.url)
            phys, _ = self.free()
            if why is None and (phys is None or phys >= self.wait_free):
                return
            self._wait(2)

    def _wait(self, s):
        self.sleep(s)
        self.waited_s += s

    def check(self) -> bool:
        """One watchdog look: True = fine; False = memory is short, the benchmark was ended."""
        phys, commit = self.free()
        low = [v for v in (phys, commit) if v is not None and v < self.kill_free]
        if low:
            self.log(f"accbench guard: free memory {phys} MB / commit {commit} MB under {self.kill_free} MB: "
                     "ending the benchmark to protect the station")
            self.close()
            self.die()
            return False
        return True

    def _watch(self):
        while not self._stop.is_set():
            if not self.check():
                return
            self._stop.wait(1.0)

    @staticmethod
    def _terminate():
        sys.stdout.flush()
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.TerminateProcess(ctypes.windll.kernel32.GetCurrentProcess(), 3)
        os._exit(3)


def _below_normal():
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
    else:
        try:
            os.nice(10)
        except OSError:
            pass


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
