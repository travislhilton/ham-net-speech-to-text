"""The accurate second pass: send recordings to the home server (transcribe/server.py, Whisper large-v3 on Apple
silicon) and keep the result BESIDE the live text. The live text is never replaced; the dashboard shows the accurate
text when there is one, and the Winlink forms use it. Progress, time left and the outcome are published so the
operator can decide whether to send the Winlink messages now (live text) or wait.

Two stages (POST /accurate/context, transcribe/context.py):
  provisional  as each transmission's live text is done: the transmission is decoded inside the ~25 s of the net
               before it (pause-split pieces of one turn joined again), one window. `accurate.stage = "provisional"`.
  final        when the net has been quiet `final_after_min` minutes (or the operator presses Run): the whole net in
               blocks of ~4 min with 30 s of context on each side, decoded twice at shifted window cuts and merged
               word by word. Revises the provisional text. `accurate.stage = "final"`.
A home server without /accurate/context (older service) gets one transmission at a time on POST /accurate, as before;
those results count as final.

Config (station.yaml):
    accurate:
      urls: [http://192.168.1.20:8090, http://my-server:8090]   # your server; tried in order
      live: true                # provisional text as each transmission ends
      final_after_min: 5        # the final pass this many quiet minutes after a net's last transmission
      auto_after_min: 15        # catch-up for anything still without the final pass (e.g. the Mac was off); 0 = off
      context: true             # false = the old one-transmission-at-a-time pass
The key: EMCOMM_STT_KEY in the environment or in config/secrets.env (KEY=value lines; never committed).
"""
import io
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_URLS: list[str] = []   # no default server: config/station.yaml accurate.urls names it
FIRST_GUESS_RATE = 8.0          # audio seconds per wall second before the first measurement (home server: 11-20x)
OVERHEAD_S = 0.4                # per transmission: upload + model call
PROV_CONTEXT_S = 26.0           # provisional: the target + the net before it, up to one Whisper window
PROV_BATCH_S = 120.0            # provisional catch-up: up to this much target audio per request
FINAL_BLOCK_S = 240.0           # final: target audio per request
FINAL_MARGIN_S = 30.0           # final: context on each side of a block


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


def _multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, name, data in files:
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                   f'Content-Type: audio/wav\r\n\r\n'.encode() + data + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


class NoContextEndpoint(Exception):
    """The home server service predates POST /accurate/context."""


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
        body, ctype = _multipart({"prompt": prompt, "highpass_hz": str(highpass_hz)}, [("audio", "tx.wav", wav)])
        req = urllib.request.Request(f"{self.url}/accurate", data=body, method="POST",
                                     headers={"Content-Type": ctype, "X-API-Key": self.key or ""})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)

    def transcribe_context(self, clips: list[dict], highpass_hz: int, stage: str) -> dict:
        """clips: [{"id", "start" (ISO), "target", "wav" (bytes)}] -> {"results": {id: {...}}, "model", ...}"""
        man = {"clips": [{"id": c["id"], "start": c["start"], "target": c["target"], "file": f"a{i}"}
                         for i, c in enumerate(clips)], "highpass_hz": highpass_hz, "stage": stage}
        body, ctype = _multipart({"manifest": json.dumps(man)},
                                 [(f"a{i}", f"{c['id']}.wav", c["wav"]) for i, c in enumerate(clips)])
        req = urllib.request.Request(f"{self.url}/accurate/context", data=body, method="POST",
                                     headers={"Content-Type": ctype, "X-API-Key": self.key or ""})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (404, 405):
                raise NoContextEndpoint() from e
            raise


def _wav_seconds(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return 0.0


def stage_of(t: dict) -> str | None:
    """'provisional' / 'final' for a transmission's accurate result (accurate or accurate_echo); None = none yet.
    A result from before the two stages existed counts as final."""
    r = t.get("accurate") or t.get("accurate_echo")
    if not r:
        return None
    return r.get("stage") or "final"


class AccuratePass:
    def __init__(self, station, cfg: dict | None = None, remote: Remote | None = None):
        cfg = cfg or {}
        self.st = station
        self.auto_after = timedelta(minutes=float(cfg.get("auto_after_min", 15)))
        self.final_after = timedelta(minutes=float(cfg.get("final_after_min", 5)))
        self.live = bool(cfg.get("live", True))
        self.cfg_prompt = cfg.get("prompt", "")
        self.remote = remote or Remote(cfg.get("urls", DEFAULT_URLS), read_key(station.root))
        self.enabled = remote is not None or bool(self.remote.key)      # no key, no uploads (tests, demos, new PCs)
        # the context endpoint: on unless configured off, the remote cannot do it, or the Mac said 404
        self.context = bool(cfg.get("context", True)) and hasattr(self.remote, "transcribe_context")
        self.jobs: dict[str, dict] = {}          # key "radio|start" (provisional) / "radio|start|final" -> status
        self.q: queue.Queue = queue.Queue()      # provisional jobs: served first
        self.qf: queue.Queue = queue.Queue()     # final jobs: a block at a time, provisional work in between
        self.rate = FIRST_GUESS_RATE
        self.lock = threading.Lock()
        self.retry_at: dict[str, float] = {}
        self.reach: bool | None = None          # the home server at the last try (None = not tried yet)

    # ----- status -----
    def status(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.jobs.values()]

    def available(self) -> bool:
        """Will waiting help? On, with a key, and the home server not known to be down."""
        return bool(self.enabled and (self.live or self.auto_after > timedelta(0)) and self.reach is not False)

    def _publish(self):
        self.st.bus.publish("accurate", {"sessions": self.status(), "available": self.available()})

    def _eta(self, j) -> int:
        left = j["audio_s"] - j["audio_done_s"]
        return int(round(left / max(self.rate, 0.5) + OVERHEAD_S * (j["total"] - j["done"] - j["failed"])))

    # ----- what to do -----
    def _span(self, rid, start, end) -> list[dict]:
        from . import ics
        return [t for t in ics.select(self.st.tx, rid, start, end, include_noise=True) if t.get("wav")]

    def _pending(self, rid, start, end, force=False, stage="final") -> list[dict]:
        span = self._span(rid, start, end)
        if force:
            return span
        if stage == "provisional":
            return [t for t in span if stage_of(t) is None]
        if not self.context:                     # the old pass has one stage: any result is the result
            return [t for t in span if stage_of(t) is None]
        return [t for t in span if stage_of(t) != "final"]

    def submit(self, rid: str, start: str, end: str, auto=False, force=False, stage="final") -> dict:
        key = f"{rid}|{start}" + ("|final" if stage == "final" else "")
        with self.st.lock:
            todo = self._pending(rid, start, end, force, stage)
        with self.lock:
            j = self.jobs.get(key)
            if j and j["state"] in ("queued", "running"):
                j["end"] = max(j["end"], end)          # a manual run of a net still going: extend it
                return dict(j)
            prev = j if j and j["state"] == "done" and not force else {"done": 0, "audio_done_s": 0.0}
            j = {"key": key, "radio": rid, "start": start, "end": end, "state": "queued", "auto": auto, "stage": stage,
                 "total": prev["done"] + len(todo), "done": prev["done"], "failed": 0,
                 "audio_s": round(prev["audio_done_s"] + sum(t.get("duration_s") or 0 for t in todo), 1),
                 "audio_done_s": prev["audio_done_s"], "queued_at": datetime.now().isoformat(timespec="seconds"),
                 "started_at": None, "finished_at": None, "eta_s": None, "error": None, "model": None, "force": force}
            if not todo:
                j.update(state="done", finished_at=j["queued_at"])
            j["eta_s"] = self._eta(j)
            self.jobs[key] = j
        if todo:
            (self.q if stage == "provisional" else self.qf).put(key)
        self._publish()
        return dict(j)

    def _session_of(self, tx):
        from . import ics
        with self.st.lock:
            ss = ics.sessions(list(self.st.tx), tx["radio"])
        return next((x for x in reversed(ss) if x["start"] <= tx["start"] <= x["end"]), None)

    def on_final(self, tx: dict):
        """A transmission's live text is done: queue it under its net/conversation's provisional job right away."""
        if not self.enabled or not self.live or not tx.get("wav"):
            return
        s = self._session_of(tx)
        if not s:
            return
        key = f"{tx['radio']}|{s['start']}"
        with self.lock:
            j = self.jobs.get(key)
            if j and j["state"] == "unreachable" and time.time() < self.retry_at.get(key, 0):
                return                                          # offline: the sweep retries later
        self.submit(tx["radio"], s["start"], s["end"], auto=True,
                    stage="provisional" if self.context else "final")

    def tick(self, now: datetime | None = None):
        """Called every few seconds: the final pass for each net/conversation that has gone quiet (final_after_min),
        and a catch-up for anything still without it auto_after_min after the net (the Mac was unreachable)."""
        if not self.enabled:
            return
        from . import ics
        now = now or datetime.now()
        with self.st.lock:
            tx = list(self.st.tx)
        for rid in self.st.radios:
            for s in ics.sessions(tx, rid):
                quiet = now - datetime.fromisoformat(s["end"])
                due = (self.context and self.final_after > timedelta(0) and quiet >= self.final_after) or \
                      (self.auto_after > timedelta(0) and quiet >= self.auto_after)
                if not due:
                    continue                                       # still going (or only just over)
                key = f"{rid}|{s['start']}|final"
                with self.lock:
                    j = self.jobs.get(key)
                    busy = [x for x in self.jobs.values() if x["radio"] == rid and x["start"] == s["start"]
                            and x["state"] in ("queued", "running")]
                if busy:
                    continue
                if j and j["state"] in ("unreachable", "error") and time.time() < self.retry_at.get(key, 0):
                    continue
                with self.st.lock:
                    missing = self._pending(rid, s["start"], s["end"])
                if missing:
                    self.submit(rid, s["start"], s["end"], auto=True)

    # ----- the worker -----
    def run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                key = self.q.get_nowait()
            except queue.Empty:
                try:
                    key = self.qf.get(timeout=0.5)
                except queue.Empty:
                    try:
                        key = self.q.get(timeout=0.5)
                    except queue.Empty:
                        continue
            self._run_job(key)

    def _drain_provisional(self):
        """Between the blocks of a long final pass: the transmissions that just ended get their text first."""
        while True:
            try:
                key = self.q.get_nowait()
            except queue.Empty:
                return
            self._run_job(key)

    def _fail(self, j, key, state, error, retry_s=120):
        with self.lock:
            j.update(state=state, error=error, finished_at=datetime.now().isoformat(timespec="seconds"))
            self.retry_at[key] = time.time() + retry_s
        self._publish()

    def _run_job(self, key):
        with self.lock:
            j = self.jobs[key]
            j.update(state="running", started_at=datetime.now().isoformat(timespec="seconds"), error=None)
        self._publish()
        if not self.enabled:
            return self._fail(j, key, "error", "no upload key: put EMCOMM_STT_KEY=... in config\\secrets.env")
        self.reach = self.remote.reachable()
        if not self.reach:
            return self._fail(j, key, "unreachable", "home server not reachable")
        stage = j.get("stage", "final")
        hp = self.st.highpass_for(j["radio"])
        tried = set()
        while True:                                            # the net may grow while this runs
            with self.st.lock:
                todo = [t for t in self._pending(j["radio"], j["start"], j["end"], j.get("force"), stage)
                        if t["id"] not in tried]
            if not todo:
                break
            with self.lock:
                j["total"] = j["done"] + j["failed"] + len(todo)
                j["audio_s"] = round(j["audio_done_s"] + sum(t.get("duration_s") or 0 for t in todo), 1)
            if self.context:
                try:
                    self._context_all(j, todo, tried, hp, stage)
                except NoContextEndpoint:
                    self.context = False                       # an older Mac service: one at a time from now on
                    print(f"{datetime.now():%H:%M:%S} accurate pass: the Mac has no /accurate/context; one transmission at a time", flush=True)
                    continue
            else:
                self._transcribe_all(j, todo, tried, self.cfg_prompt, hp)
            if self.st.stop.is_set():
                return
        with self.lock:
            j.update(state="done" if j["done"] or not j["failed"] else "error", eta_s=0,
                     finished_at=datetime.now().isoformat(timespec="seconds"))
            if j["state"] == "error":
                self.retry_at[key] = time.time() + 120
        if not j["auto"] or self.st.stop.is_set():
            print(f"{datetime.now():%H:%M:%S} accurate pass {key}: {j['done']} of {j['total']} done, {j['failed']} failed", flush=True)
        self._publish()

    # ----- one at a time (POST /accurate) -----
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

    # ----- with context (POST /accurate/context) -----
    def _requests(self, j, todo, stage) -> list[tuple[list[dict], list[dict]]]:
        """-> [(clips to send in time order with their target flag, the targets)]. Provisional: each run of targets
        (up to PROV_BATCH_S) with the PROV_CONTEXT_S of the net before it. Final: blocks of FINAL_BLOCK_S of target
        audio with FINAL_MARGIN_S of context on each side."""
        with self.st.lock:
            span = sorted(self._span(j["radio"], j["start"], j["end"]), key=lambda t: t["start"])
        ids = {t["id"] for t in todo}
        idx = {t["id"]: i for i, t in enumerate(span)}
        dur = lambda t: float(t.get("duration_s") or 0)
        out, cur, cur_s = [], [], 0.0
        limit = PROV_BATCH_S if stage == "provisional" else FINAL_BLOCK_S
        for t in span:
            if t["id"] not in ids:
                continue
            if cur and (cur_s + dur(t) > limit or idx[t["id"]] - idx[cur[-1]["id"]] > 8):
                out.append(cur); cur, cur_s = [], 0.0
            cur.append(t); cur_s += dur(t)
        if cur:
            out.append(cur)
        reqs = []
        for group in out:
            i0, i1 = idx[group[0]["id"]], idx[group[-1]["id"]]
            before_s = PROV_CONTEXT_S - min(dur(group[0]), PROV_CONTEXT_S) if stage == "provisional" else FINAL_MARGIN_S
            after_s = 0.0 if stage == "provisional" else FINAL_MARGIN_S
            k, s = i0 - 1, 0.0
            while k >= 0 and s + dur(span[k]) <= max(before_s, 0.0):
                s += dur(span[k]); k -= 1
            m, s = i1 + 1, 0.0
            while m < len(span) and s + dur(span[m]) <= after_s:
                s += dur(span[m]); m += 1
            gids = {t["id"] for t in group}
            clips = [{"tx": t, "target": t["id"] in gids} for t in span[k + 1:m]]
            reqs.append((clips, group))
        return reqs

    def _context_all(self, j, todo, tried, hp, stage):
        for clips, targets in self._requests(j, todo, stage):
            if self.st.stop.is_set():
                return
            if stage == "final":
                self._drain_provisional()
            for t in targets:
                tried.add(t["id"])
            try:
                send = []
                for c in clips:
                    t = c["tx"]
                    send.append({"id": t["id"], "start": t["start"], "target": c["target"],
                                 "wav": (self.st.data / t["wav"].replace("\\", "/")).read_bytes()})
                t0 = time.time()
                res = self.remote.transcribe_context(send, hp, stage)
                took = max(time.time() - t0, 0.05)
                sent_s = sum(_wav_seconds(c["wav"]) for c in send)
                self.rate = 0.7 * self.rate + 0.3 * (sent_s / took)
                got = res.get("results") or {}
                for t in targets:
                    r = got.get(t["id"])
                    if r is None:
                        with self.lock:
                            j["failed"] += 1; j["error"] = f"no result for {t['id']}"
                        continue
                    self.st.apply_accurate(t["id"], {**r, "model": res.get("model"), "stage": res.get("stage", stage)})
                    with self.lock:
                        j["done"] += 1; j["model"] = res.get("model")
            except NoContextEndpoint:
                for t in targets:
                    tried.discard(t["id"])
                raise
            except (OSError, ValueError, urllib.error.URLError) as e:
                with self.lock:
                    j["failed"] += len(targets); j["error"] = f"{type(e).__name__}: {e}"[:200]
            with self.lock:
                j["audio_done_s"] += sum(float(t.get("duration_s") or 0) for t in targets)
                j["eta_s"] = self._eta(j)
            self._publish()
