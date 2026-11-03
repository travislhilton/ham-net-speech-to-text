"""The accurate second pass: after a net or conversation ends (or when the operator asks), send its recordings to
the home server (transcribe/server.py POST /accurate, Whisper large-v3 on Apple silicon) and keep the result BESIDE
the live text. The live text is never replaced; the dashboard shows the accurate text when there is one, and the
Winlink forms use it. Progress, time left and the outcome are published so the operator can decide whether to send
the Winlink messages now (live text) or wait.

Config (station.yaml):
    accurate:
      urls: [http://192.168.1.54:8090, http://home-server:8090]   # tried in order
      live: true                # re-transcribe each transmission as soon as its live text is done
      auto_after_min: 15        # catch-up: anything still without the pass this many quiet minutes after a net
                                # (e.g. the home server was unreachable); 0 = off
The key: EMCOMM_STT_KEY in the environment or in config/secrets.env (KEY=value lines; never committed).
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_URLS = ["http://192.168.1.54:8090", "http://home-server:8090"]
FIRST_GUESS_RATE = 8.0          # audio seconds per wall second before the first measurement (home server: 11-20x)
OVERHEAD_S = 0.4                # per transmission: upload + model call


def read_key(root: Path) -> str | None:
    if os.environ.get("EMCOMM_STT_KEY"):
        return os.environ["EMCOMM_STT_KEY"]
    try:
        for line in (root / "config" / "secrets.env").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "EMCOMM_STT_KEY" and v.strip():
                return v.strip()
    except OSError:
        pass
    return None


def _multipart(fields: dict, file_name: str, data: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{b}\r\nContent-Disposition: form-data; name="audio"; filename="{file_name}"\r\n'
               f'Content-Type: audio/wav\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


class Remote:
    """The home server service. Swappable in tests."""
    def __init__(self, urls, key):
        self.urls, self.key, self.url = list(urls), key, None

    def reachable(self) -> bool:
        for u in self.urls:
            try:
                with urllib.request.urlopen(f"{u}/health", timeout=4) as r:
                    if json.load(r).get("ok"):
                        self.url = u
                        return True
            except (OSError, ValueError):
                continue
        return False

    def transcribe(self, wav: bytes, prompt: str, highpass_hz: int) -> dict:
        body, ctype = _multipart({"prompt": prompt, "highpass_hz": str(highpass_hz)}, "tx.wav", wav)
        req = urllib.request.Request(f"{self.url}/accurate", data=body, method="POST",
                                     headers={"Content-Type": ctype, "X-API-Key": self.key or ""})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)


class AccuratePass:
    def __init__(self, station, cfg: dict | None = None, remote: Remote | None = None):
        cfg = cfg or {}
        self.st = station
        self.auto_after = timedelta(minutes=float(cfg.get("auto_after_min", 15)))
        self.live = bool(cfg.get("live", True))
        self.remote = remote or Remote(cfg.get("urls", DEFAULT_URLS), read_key(station.root))
        self.enabled = remote is not None or bool(self.remote.key)      # no key, no uploads (tests, demos, new PCs)
        self.jobs: dict[str, dict] = {}          # key "radio|start" -> status
        self.q: queue.Queue = queue.Queue()
        self.rate = FIRST_GUESS_RATE
        self.lock = threading.Lock()
        self.retry_at: dict[str, float] = {}

    # ----- status -----
    def status(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.jobs.values()]

    def _publish(self):
        self.st.bus.publish("accurate", {"sessions": self.status()})

    def _eta(self, j) -> int:
        left = j["audio_s"] - j["audio_done_s"]
        return int(round(left / max(self.rate, 0.5) + OVERHEAD_S * (j["total"] - j["done"] - j["failed"])))

    # ----- what to do -----
    def _pending(self, rid, start, end, force=False) -> list[dict]:
        from . import ics
        span = [t for t in ics.select(self.st.tx, rid, start, end, include_noise=True) if t.get("wav")]
        return span if force else [t for t in span if not t.get("accurate")]

    def submit(self, rid: str, start: str, end: str, auto=False, force=False) -> dict:
        key = f"{rid}|{start}"
        with self.st.lock:
            todo = self._pending(rid, start, end, force)
        with self.lock:
            j = self.jobs.get(key)
            if j and j["state"] in ("queued", "running"):
                j["end"] = max(j["end"], end)          # a manual run of a net still going: extend it
                return dict(j)
            prev = j if j and j["state"] == "done" and not force else {"done": 0, "audio_done_s": 0.0}
            j = {"key": key, "radio": rid, "start": start, "end": end, "state": "queued", "auto": auto,
                 "total": prev["done"] + len(todo), "done": prev["done"], "failed": 0,
                 "audio_s": round(prev["audio_done_s"] + sum(t.get("duration_s") or 0 for t in todo), 1),
                 "audio_done_s": prev["audio_done_s"], "queued_at": datetime.now().isoformat(timespec="seconds"),
                 "started_at": None, "finished_at": None, "eta_s": None, "error": None, "model": None, "force": force}
            if not todo:
                j.update(state="done", finished_at=j["queued_at"])
            j["eta_s"] = self._eta(j)
            self.jobs[key] = j
        if todo:
            self.q.put(key)
        self._publish()
        return dict(j)

    def on_final(self, tx: dict):
        """A transmission's live text is done: queue it under its net/conversation's job right away."""
        if not self.enabled or not self.live or not tx.get("wav"):
            return
        from . import ics
        with self.st.lock:
            ss = ics.sessions(list(self.st.tx), tx["radio"])
        s = next((x for x in reversed(ss) if x["start"] <= tx["start"] <= x["end"]), None)
        if not s:
            return
        key = f"{tx['radio']}|{s['start']}"
        with self.lock:
            j = self.jobs.get(key)
            if j and j["state"] == "unreachable" and time.time() < self.retry_at.get(key, 0):
                return                                          # offline: the sweep retries later
        self.submit(tx["radio"], s["start"], s["end"], auto=True)

    def tick(self, now: datetime | None = None):
        """Called every few seconds: queue each net/conversation that has gone quiet and still lacks the pass."""
        if not self.enabled or self.auto_after <= timedelta(0):
            return
        from . import ics
        now = now or datetime.now()
        with self.st.lock:
            tx = list(self.st.tx)
        for rid in self.st.radios:
            for s in ics.sessions(tx, rid):
                if now - datetime.fromisoformat(s["end"]) < self.auto_after:
                    continue                                       # still going (or only just over)
                key = f"{rid}|{s['start']}"
                with self.lock:
                    j = self.jobs.get(key)
                if j and j["state"] in ("queued", "running"):
                    continue
                if j and j["state"] == "unreachable" and time.time() < self.retry_at.get(key, 0):
                    continue
                with self.st.lock:
                    missing = self._pending(rid, s["start"], s["end"])
                if missing:
                    self.submit(rid, s["start"], s["end"], auto=True)

    # ----- the worker -----
    def run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                key = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            self._run_job(key)

    def _run_job(self, key):
        with self.lock:
            j = self.jobs[key]
            j.update(state="running", started_at=datetime.now().isoformat(timespec="seconds"), error=None)
        self._publish()
        if not self.enabled:
            with self.lock:
                j.update(state="error", error="no upload key: put EMCOMM_STT_KEY=... in config\\secrets.env",
                         finished_at=datetime.now().isoformat(timespec="seconds"))
            self._publish()
            return
        if not self.remote.reachable():
            with self.lock:
                j.update(state="unreachable", error="home server not reachable", finished_at=datetime.now().isoformat(timespec="seconds"))
                self.retry_at[key] = time.time() + 120
            self._publish()
            return
        prompt = self.st.prompt_for(j["radio"], own_call=False)       # no call in it: large-v3 echoes it
        hp = self.st.highpass_for(j["radio"])
        tried = set()
        while True:                                            # the net may grow while this runs
            with self.st.lock:
                todo = [t for t in self._pending(j["radio"], j["start"], j["end"], j.get("force")) if t["id"] not in tried]
            if not todo:
                break
            with self.lock:
                j["total"] = j["done"] + j["failed"] + len(todo)
                j["audio_s"] = round(j["audio_done_s"] + sum(t.get("duration_s") or 0 for t in todo), 1)
            self._transcribe_all(j, todo, tried, prompt, hp)
            if self.st.stop.is_set():
                return
        with self.lock:
            j.update(state="done" if j["done"] or not j["failed"] else "error", eta_s=0,
                     finished_at=datetime.now().isoformat(timespec="seconds"))
        if not j["auto"] or self.st.stop.is_set():
            print(f"{datetime.now():%H:%M:%S} accurate pass {key}: {j['done']} of {j['total']} done, {j['failed']} failed", flush=True)
        self._publish()

    def _transcribe_all(self, j, todo, tried, prompt, hp):
        for t in todo:
            tried.add(t["id"])
            if self.st.stop.is_set():
                return
            try:
                wav = (self.st.data / t["wav"].replace("\\", "/")).read_bytes()
                t0 = time.time()
                res = self.remote.transcribe(wav, prompt, hp)
                took = max(time.time() - t0, 0.05)
                self.rate = 0.7 * self.rate + 0.3 * (res.get("audio_s", t.get("duration_s") or 0) / took)
                self.st.apply_accurate(t["id"], res)
                with self.lock:
                    j["done"] += 1; j["model"] = res.get("model")
            except (OSError, ValueError, urllib.error.URLError) as e:
                with self.lock:
                    j["failed"] += 1; j["error"] = f"{type(e).__name__}: {e}"[:200]
            with self.lock:
                j["audio_done_s"] += t.get("duration_s") or 0
                j["eta_s"] = self._eta(j)
            self._publish()
