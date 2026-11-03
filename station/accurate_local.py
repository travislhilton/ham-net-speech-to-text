"""The accurate pass on THIS PC: the same method as the home server (transcribe/context.py) on the station's own spare
compute, for operators without the home server (another network, or no internet at all).

    tools/pick_accurate.py (run by the installer) measures what the PC can sustain and writes engine.yaml `accurate:`
        {mode: both | final_only | none, engine, model, device, rtf, ...}
    station.yaml `accurate: {local: auto | final_only | off, local_on_battery: false, quiet_s: 2, unload_after_min: 10}`

The work runs in a CHILD process (`python -m station.accurate_local`) at below-normal priority, never in the station:
  - the live text always wins: the station holds the child while a transmission is being received or a live final
    is queued or transcribing (and for `quiet_s` after); the child stops between ~30 s windows
    (transcribe/decoders.py `pause`), so at most one window is ever in flight when a transmission starts;
  - it has its own OpenVINO pipeline (never the live engine's object) at LOW GPU queue priority;
  - it never creates an openvino.Core (a Core beside a GPU WhisperPipeline hangs a process at exit, CLAUDE.md) and
    is ended with TerminateProcess, never a normal exit (the GPU plugin can hang while unloading);
  - its model is loaded on the first job and the process ended after `unload_after_min` idle (frees the RAM);
  - on a laptop on battery it is held unless `local_on_battery: true`.

Protocol (one JSON object per line): station -> child {"op": "run", "id", "clips": [{"id", "start" (epoch s),
"target", "path"}], "highpass_hz", "method"} | {"op": "hold"} | {"op": "go"}; child -> station {"ready": model} then
{"id", "results", "busy_s", "held_s", "audio_s"} or {"id", "error"}.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHERE = "this PC"
PROV_RTF_MAX = 0.12      # provisional: ~26 s decoded per transmission -> <= ~3 s of work each, between transmissions
FINAL_RTF_MAX = 1.0      # final: ~2.5x the net's audio is decoded (two decodes + context) -> <= ~2.5x the net's audio
                         # (i5-12600H + Iris Xe: large-v3 rtf 0.62-0.68; a net with 30 min of speech: final ~50 min on)


class LocalError(OSError):
    """The local worker failed (caught like a network error by AccuratePass: the job fails and is retried)."""


def label(spec: dict) -> str:
    return f"{spec['model']}@{spec['device']} ({WHERE})"


def decide(measured: list[dict], live_device: str | None = None) -> dict:
    """The benchmark's rows -> what this PC runs locally. Rows: {"engine", "model", "device", "rtf"} (rtf = seconds of
    work per second of net audio, one decode; None/absent = failed).
    Model: large-v3 if any device runs it at <= FINAL_RTF_MAX (the home server's model), else turbo (i5-12600H +
    Iris Xe, synthetic FM net, final pass: large-v3 29.0 % WER, turbo 38.8 %, live text 40.7 %: turbo is the last
    resort), else none. Device: an NPU the live final is not on wins when it is at most twice as slow as the
    fastest (no contention); otherwise the fastest. NOT the CPU for contention's sake: on the i5-12600H the pass on
    the CPU slowed the live final on the GPU more (median 3.03 -> 3.46 s) than the pass on the same GPU did
    (3.06 -> 3.24 s), and took more memory. Provisional too when the chosen one is fast enough for every
    transmission (<= PROV_RTF_MAX)."""
    ok = [r for r in measured if r.get("rtf") is not None]
    for pref in ("large-v3", "turbo"):
        rows = [r for r in ok if (pref in r["model"]) and ("turbo" not in r["model"] or pref == "turbo")
                and r["rtf"] <= FINAL_RTF_MAX]
        if not rows:
            continue
        best = min(rows, key=lambda r: r["rtf"])
        apart = [r for r in rows if r["device"].startswith("NPU") and r["device"] != live_device
                 and r["rtf"] <= 2 * best["rtf"]]
        pick = min(apart, key=lambda r: r["rtf"]) if apart else best
        mode = "both" if pick["rtf"] <= PROV_RTF_MAX else "final_only"
        return {"mode": mode, **{k: pick[k] for k in ("engine", "model", "device", "rtf")},
                **({"ram_mb": pick["ram_mb"]} if pick.get("ram_mb") else {})}
    return {"mode": "none"}


def on_ac_power() -> bool:
    """False only when Windows says the PC runs on its battery (a desktop, an unknown state or another OS: True)."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        s = SPS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            return True
        return s.ACLineStatus != 0                       # 0 = offline (battery), 1 = AC, 255 = unknown
    except Exception:  # noqa: BLE001
        return True


def free_commit_mb() -> int | None:
    """Memory Windows can still promise (RAM + page file not yet committed), MB; None elsewhere or unknown.
    An i5-12600H station PC (16 GB, 1 GB page file): ~6 GB free with the live station running; large-v3 on the
    GPU takes ~3.8 GB."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                       [(n, ctypes.c_ulonglong) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                                                          "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual",
                                                          "ullAvailExtendedVirtual")]
        m = MS(); m.dwLength = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return int(m.ullAvailPageFile >> 20)
    except Exception:  # noqa: BLE001
        pass
    return None


MEMORY_MARGIN_MB = 1024     # left for the live station and Windows after the local model is loaded


class Local:
    """AccuratePass's backend on this PC (the same calls as accurate.Remote). `spec` = engine.yaml `accurate:`."""
    where = WHERE

    def __init__(self, spec: dict, busy=lambda: False, cfg: dict | None = None, models_dir: Path | None = None,
                 data_dir: Path | None = None, command: list[str] | None = None, power=on_ac_power,
                 free_mb=free_commit_mb):
        cfg = cfg or {}
        self.spec = dict(spec)
        self.model = label(self.spec)
        want = str(cfg.get("local", "auto")).lower()
        self.provisional = self.spec.get("mode") == "both" and want != "final_only"
        self.busy, self.power, self.free_mb = busy, power, free_mb
        self.on_battery_ok = bool(cfg.get("local_on_battery", False))
        self.quiet_s = float(cfg.get("quiet_s", 2.0))
        self.unload_after = float(cfg.get("unload_after_min", 10)) * 60
        self.models_dir = Path(models_dir or ROOT / "models")
        self.data_dir = Path(data_dir or ROOT / "data")
        self.command = command or [sys.executable, "-m", "station.accurate_local"]
        rtf = float(self.spec.get("rtf") or 0.5)
        # audio seconds sent per wall second before the first measurement: provisional ~1 decode, final ~2 + context
        self.first_rate = 1.0 / max(rtf * 2.2, 0.02)
        self.proc = None
        self.lines: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.held = None                         # what the child was last told
        self.want_hold = True                    # what it should be told (the watcher's verdict)
        self.hold_reason = ""
        self.last_used = time.time()
        self.n = 0
        self._stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True, name="accurate-local-watch").start()

    # ----- the AccuratePass interface -----
    def reachable(self) -> bool:
        return True

    def transcribe_context(self, clips: list[dict], highpass_hz: int, stage: str) -> dict:
        """clips: [{"id", "start" (ISO), "target", "path"}] -> {"results", "model", "stage", "where", ...}."""
        man = [{"id": c["id"], "start": datetime.fromisoformat(c["start"]).timestamp(), "target": bool(c["target"]),
                "path": str(c["path"])} for c in clips]
        with self.lock:                                  # one request at a time (AccuratePass has one worker)
            self._ensure()
            self._tell(self.want_hold)
            self.n += 1
            rid = str(self.n)
            self._send({"op": "run", "id": rid, "clips": man, "highpass_hz": int(highpass_hz or 0),
                        "method": "both" if stage == "final" else "long"})
            while True:
                msg = self._read(None)
                if msg.get("id") == rid:
                    break
            self.last_used = time.time()
        if "error" in msg:
            raise LocalError(msg["error"])
        return {"results": msg.get("results") or {}, "model": self.model, "stage": stage, "where": WHERE,
                "audio_s": msg.get("audio_s"), "elapsed_s": msg.get("busy_s"), "held_s": msg.get("held_s"),
                "method": f"context-{'both' if stage == 'final' else 'long'}"}

    def close(self):
        self._stop.set()
        self._kill()

    # ----- hold / go -----
    def should_hold(self) -> str:
        """'' = may run; else why not."""
        if not self.on_battery_ok and not self.power():
            return "on battery"
        if self.busy():
            return "live text"
        return ""

    def _watch(self):
        quiet_since = None
        while not self._stop.wait(0.2):
            why = self.should_hold()
            now = time.time()
            if why:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            hold = bool(why) or now - quiet_since < self.quiet_s
            self.want_hold, self.hold_reason = hold, why
            if self.proc and self.proc.poll() is None and hold != self.held:
                try:
                    self._tell(hold)
                except OSError:
                    pass
            if self.proc and not self.lock.locked() and now - self.last_used > self.unload_after:
                with self.lock:
                    self._kill()                            # idle: give the RAM back; the next job loads it again

    # ----- the child process -----
    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        self._kill()
        need = int(self.spec.get("ram_mb") or 4000) + MEMORY_MARGIN_MB
        free = self.free_mb()
        if free is not None and free < need:          # never squeeze the live station: wait (retried in 2 min)
            raise LocalError(f"not enough free memory for the local accurate model ({free} MB free, {need} MB needed)")
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00004000 | 0x08000000      # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
        else:
            kw["preexec_fn"] = lambda: os.nice(10)
        spec = json.dumps({**self.spec, "models_dir": str(self.models_dir),
                           "cache_dir": str(self.data_dir / "ov-cache")})
        self.data_dir.mkdir(parents=True, exist_ok=True)
        err = open(self.data_dir / "accurate_local.log", "a", encoding="utf-8")   # the worker's own messages
        err.write(f"--- {datetime.now().isoformat(timespec='seconds')} {self.model}\n"); err.flush()
        self.proc = subprocess.Popen(self.command + [spec], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=err, cwd=str(ROOT), env=env, text=True, encoding="utf-8", bufsize=1, **kw)
        err.close()
        self.lines = queue.Queue()
        p, q = self.proc, self.lines

        def pump():
            for line in p.stdout:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        q.put(json.loads(line))
                    except ValueError:
                        pass
            q.put({"eof": True})
        threading.Thread(target=pump, daemon=True, name="accurate-local-out").start()
        self.held = None                                     # the watcher sends the current state
        t0 = time.time()
        msg = self._read(900)                                # first load compiles the model for the GPU: minutes
        if "ready" not in msg:
            self._kill()
            raise LocalError(f"local accurate model did not load: {msg.get('error') or msg}")
        print(f"{datetime.now():%H:%M:%S} accurate pass: {self.model} loaded in {time.time() - t0:.0f} s", flush=True)

    def _tell(self, hold: bool):
        self._send({"op": "hold" if hold else "go"})
        self.held = hold

    def _send(self, msg):
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n"); self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            raise LocalError(f"local accurate worker: {e}") from e

    def _read(self, timeout):
        try:
            msg = self.lines.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            raise LocalError("local accurate worker did not answer") from None
        if msg.get("eof"):
            p = self.proc
            try:
                rc = p.wait(5) if p else None
            except subprocess.TimeoutExpired:
                rc = None
            self._kill()
            raise LocalError(f"local accurate worker ended (exit {rc}; see data\\accurate_local.log)")
        return msg

    def _kill(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.kill()                                     # TerminateProcess on Windows: no GPU-unload hang
            except OSError:
                pass


def make(station, cfg: dict | None, spec: dict | None) -> Local | None:
    """The local backend, or None: off in station.yaml, no benchmark result, or the benchmark said none."""
    cfg = cfg or {}
    if not spec or spec.get("mode") not in ("both", "final_only"):
        return None
    if str(cfg.get("local", "auto")).lower() in ("off", "false", "no", "0"):
        return None
    return Local(spec, busy=station.live_busy, cfg=cfg, data_dir=station.data)


# ---------------------------------------------------------------- the child process
def serve(spec: dict, inp=None, out=None, decode=None, on_eof=None):
    """Read requests, decode, answer. `decode` (tests) replaces the real recogniser. When the station's end of the
    pipe closes (the station stopped or was killed) `on_eof` runs: the real worker ends at once, even mid-decode or
    held."""
    import soundfile as sf
    from transcribe import context as cx
    inp, out = inp or sys.stdin, out or sys.stdout
    go = threading.Event(); go.set()
    reqs: queue.Queue = queue.Queue()
    held = [0.0]

    def reader():
        for line in inp:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("op") == "hold":
                go.clear()
            elif m.get("op") == "go":
                go.set()
            elif m.get("op") == "run":
                reqs.put(m)
        if on_eof:
            on_eof()
        go.set()
        reqs.put(None)
    threading.Thread(target=reader, daemon=True).start()

    def pause():
        if not go.is_set():
            t = time.time()
            go.wait()
            held[0] += time.time() - t

    def say(m):
        out.write(json.dumps(m) + "\n"); out.flush()

    try:
        if decode is None:
            from transcribe import decoders
            decode = decoders.make(spec, Path(spec.get("models_dir", ROOT / "models")), pause=pause,
                                   cache_dir=spec.get("cache_dir"))
        else:
            inner = decode
            decode = lambda x, prompt="": (pause(), inner(x, prompt))[1]
    except Exception as e:  # noqa: BLE001
        say({"error": f"{type(e).__name__}: {e}"[:300]})
        return
    say({"ready": label(spec)})
    while True:
        m = reqs.get()
        if m is None:
            return
        try:
            clips = []
            for c in m["clips"]:
                x, sr = sf.read(c["path"], dtype="float32")
                x = x.mean(axis=1) if x.ndim > 1 else x
                if sr != cx.SR:
                    raise ValueError(f"{c['path']}: {sr} Hz")
                clips.append(cx.Clip(str(c["id"]), float(c["start"]), cx.highpass(x, m.get("highpass_hz", 0)),
                                     bool(c.get("target", True))))
            held[0] = 0.0
            t0 = time.time()
            res = cx.transcribe_clips(clips, decode, mode=m.get("method", "both"))
            el = time.time() - t0
            say({"id": m["id"], "results": res, "busy_s": round(el - held[0], 2), "held_s": round(held[0], 2),
                 "audio_s": round(sum(len(c.audio) for c in clips) / cx.SR, 2)})
        except Exception as e:  # noqa: BLE001 — one bad request never ends the worker
            say({"id": m.get("id"), "error": f"{type(e).__name__}: {e}"[:300]})


def _hard_exit(rc=0):
    sys.stdout.flush(); sys.stderr.flush()
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        k.TerminateProcess(k.GetCurrentProcess(), rc)
    os._exit(rc)


if __name__ == "__main__":
    serve(json.loads(sys.argv[1]), on_eof=lambda: _hard_exit(0))
    _hard_exit(0)
