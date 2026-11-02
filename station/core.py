"""The station's moving parts: radio listeners -> partial + final transcription workers -> enrichment
(callsigns, cards, glossary, hints, net type) -> state + event stream + daily log."""
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

from hamkit import glossary
from hamkit.bands import band
from hamkit.callsigns import load_roster, normalize
from hamkit.hints import Hinter, phonetic
from .meter import Meter
from .segmenter import Segmenter

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HAMNET_DATA", ROOT / "data"))
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SR = 16000
FRAME = 512                       # 32 ms @ 16 kHz


def load_config(path: Path = ROOT / "config" / "station.yaml") -> dict:
    c = yaml.safe_load(path.read_text()) if path.exists() else {}
    c = c or {}
    c.setdefault("callsign", "")
    c.setdefault("radios", [])
    c.setdefault("schedule", [])
    c.setdefault("port", 8091)
    c.setdefault("bind", "0.0.0.0")
    c.setdefault("qrz_online", True)
    for r in c["radios"]:
        r.setdefault("id", re.sub(r"\W+", "-", r.get("name", "radio")).lower())
        r.setdefault("gate", "carrier")
    return c


def speaker_of(text: str, calls: list[str]) -> str | None:
    """Hams identify at the END ('…, this is W9XYZ') or with 'this is' / 'de'. Best guess, shown as such."""
    if not calls:
        return None
    for m in re.finditer(r"\b(?:this is|de|from)\s+([A-Z0-9/]{3,10})", text, re.I):
        if m.group(1).upper() in calls:
            return m.group(1).upper()
    return calls[-1]


class RawRing:
    """Everything a radio hears, in one-minute WAVs, kept for `keep_min` minutes (0 = off). When a
    transmission is missed or garbled, this is the evidence (gate decisions are in station.log)."""
    def __init__(self, folder: Path, keep_min: int = 60):
        self.dir, self.keep, self.buf, self.t0 = Path(folder), keep_min, [], None
        if keep_min > 0:
            self.dir.mkdir(parents=True, exist_ok=True)

    def add(self, frame: np.ndarray):
        if self.keep <= 0:
            return
        now = datetime.now()
        if self.t0 is None:
            self.t0 = now
        self.buf.append(frame)
        if now.minute != self.t0.minute or len(self.buf) * FRAME >= SR * 60:
            self.flush(); self.t0 = now

    def flush(self):
        if not self.buf:
            return
        sf.write(self.dir / f"{self.t0:%Y%m%d-%H%M%S}.wav", np.concatenate(self.buf), SR, subtype="PCM_16")
        self.buf = []
        files = sorted(self.dir.glob("*.wav"))
        for old in files[:-self.keep]:
            try:
                old.unlink()
            except OSError:
                pass


class Bus:
    def __init__(self):
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, kind: str, data: dict):
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait((kind, data))
                except queue.Full:
                    self.subs.remove(q)            # a stalled browser is dropped, never the station


class Station:
    def __init__(self, config: dict, final_engine_factory, partial_engine_factory=None, callbook=None,
                 data_dir: Path = DATA, clock=datetime.now):
        self.cfg, self.clock = config, clock
        self.me = (config.get("callsign") or "").upper()
        self.data = Path(data_dir)
        self.bus = Bus()
        self.callbook = callbook
        self.roster = load_roster() | {self.me} - {""}
        if callbook:
            callbook.roster |= self.roster
        self.lock = threading.RLock()
        self.tx: list[dict] = []
        self.heard: dict[str, dict] = {}
        # each radio is its own stream: its own net, its own guidance (two nets never blend)
        self.hinters = {r["id"]: Hinter(self.me) for r in config["radios"]}
        self.hints: dict = {r["id"]: {"cards": [], "reminders": [], "net": self.hinters[r["id"]].classify()}
                            for r in config["radios"]}
        self.meters = {r["id"]: self.make_meter(r) for r in config["radios"]}
        self.radios = {r["id"]: {"id": r["id"], "name": r.get("name", r["id"]), "device": r.get("device"),
                                 "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode", "FM"),
                                 "band": band(r.get("freq_mhz")), "gate": r["gate"], "state": "starting",
                                 "level_db": None, "carrier": False, "error": None,
                                 "meter": self.meters[r["id"]].limits()}
                       for r in config["radios"]}
        self._apply_operator()
        self.final_q: queue.Queue = queue.Queue()
        self.partial_slot: dict[str, tuple] = {}
        self.partial_ev = threading.Event()
        self._final_factory, self._partial_factory = final_engine_factory, partial_engine_factory
        self.engines = {"final": None, "partial": None, "status": "loading"}
        self.stop = threading.Event()
        self.next_id = int(time.time() * 1000)

    # ---------- lifecycle ----------
    def start(self, listen=True):
        self.load_today()
        threading.Thread(target=self._final_worker, daemon=True, name="final").start()
        if self._partial_factory:
            threading.Thread(target=self._partial_worker, daemon=True, name="partial").start()
        if listen:
            for r in self.cfg["radios"]:
                threading.Thread(target=self._listen, args=(r,), daemon=True, name=f"radio-{r['id']}").start()
        threading.Thread(target=self._ticker, daemon=True, name="ticker").start()

    def _ticker(self):
        while not self.stop.wait(1.0):
            with self.lock:
                radios = list(self.radios.values())
            self.bus.publish("radios", {"radios": radios, "net": self.schedule_now()})
            changed = False
            for rid, h in self.hinters.items():
                rem = h.reminders(self.clock())
                if rem != self.hints[rid].get("reminders"):
                    self.hints[rid]["reminders"] = rem; changed = True
            if changed:
                self.bus.publish("hints", self.hints)

    # ---------- operator settings (set on the dashboard; the operator tunes the radio) ----------
    MODES = ("FM", "USB", "LSB", "AM", "CW", "DIG")

    def _operator_path(self) -> Path:
        return self.data / "operator.json"

    def _saved(self) -> dict:
        try:
            return json.loads(self._operator_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, saved: dict):
        self._operator_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = self._operator_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8"); os.replace(tmp, self._operator_path())

    def operator(self) -> dict:
        from . import winlink
        op = {"callsign": self.me, "name": "", "grid": "", "winlink_dir": "", "winlink_account": ""} | \
             {k: v for k, v in (self._saved().get("operator") or {}).items() if v is not None}
        op["callsign"] = self.me
        installs = winlink.find_installs([op["winlink_dir"]] if op["winlink_dir"] else [])
        if not op["winlink_dir"] and installs:
            op["winlink_dir"] = installs[0]
        info = [winlink.describe(i) for i in installs]
        cur = next((i for i in info if i["path"].lower() == (op["winlink_dir"] or "").lower()), None)
        if cur and not op["winlink_account"]:
            op["winlink_account"] = cur["active"] if cur["active"] in cur["accounts"] else (cur["accounts"] or [""])[0]
        return op | {"installs": info, "express_running": winlink.express_running()}

    def set_operator(self, body: dict) -> dict:
        """The operator's own settings, set on the dashboard: callsign (hints, IDs, Winlink sender), name,
        grid, which Winlink Express install and which callsign account in it."""
        call = str(body.get("callsign", self.me) or "").upper().strip()
        if call and not re.fullmatch(r"[A-Z0-9]{3,8}(-\d{1,2})?", call):
            raise ValueError("callsign looks wrong")
        grid = str(body.get("grid", "") or "").strip()
        if grid and not re.fullmatch(r"[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?", grid):
            raise ValueError("grid square must look like EM59 or EM59et")
        with self.lock:
            saved = self._saved()
            op = saved.get("operator") or {}
            op.update(callsign=call, name=str(body.get("name", op.get("name", "")))[:80], grid=grid,
                      winlink_dir=str(body.get("winlink_dir", op.get("winlink_dir", "")))[:260],
                      winlink_account=str(body.get("winlink_account", op.get("winlink_account", ""))).upper()[:12])
            saved["operator"] = op
            self._save(saved)
            self._apply_callsign(call)
        return self.operator()

    def _apply_callsign(self, call: str):
        if call and call != self.me:
            self.me = call
            self.roster.add(call)
            for h in self.hinters.values():
                h.me = call
            if self.callbook:
                self.callbook.roster.add(call)

    # ---------- Winlink ----------
    def winlink_preview(self, rid: str, start: str | None = None, end: str | None = None, title: str | None = None) -> dict:
        from . import ics
        if rid not in self.radios:
            raise KeyError(rid)
        with self.lock:
            tx = list(self.tx)
        if not (start and end):
            b = ics.session_bounds(tx, rid)
            if not b:
                raise ValueError("no transcribed transmissions on this radio yet")
            start, end = b
        span = ics.select(tx, rid, start, end)
        op = self.operator()
        me = {c.split("-")[0] for c in (self.me, op.get("winlink_account")) if c}
        people = ics.participants(span, me, self.heard)
        net = (self.hints.get(rid) or {}).get("net") or {}
        sched = self._current_by_radio(datetime.fromisoformat(start)).get(rid) or {}
        d = ics.drafts(span, self.radios[rid], op, people, net.get("type"), title or sched.get("name"))
        return {"radio": rid, "start": start, "end": end, "count": len(span), "to": people, "drafts": d,
                "operator": {k: op[k] for k in ("callsign", "name", "winlink_dir", "winlink_account", "express_running")}}

    def winlink_send(self, body: dict) -> dict:
        """Build the chosen drafts from the installed templates and place them in the account's Outbox."""
        from . import winlink
        op = self.operator()
        install, account = op.get("winlink_dir"), op.get("winlink_account")
        if not install or not account:
            raise ValueError("set the Winlink Express folder and callsign account in Settings first")
        to = [c.upper() for c in body.get("to", []) if re.fullmatch(r"[A-Za-z0-9]{3,8}(-\d{1,2})?", str(c))]
        if not to:
            raise ValueError("choose at least one recipient")
        msgs = []
        for d in body.get("drafts", []):
            if d.get("form") not in winlink.TEMPLATES:
                continue
            t = winlink.load_template(install, d["form"])
            msgs.append(winlink.build(d["form"], t, d.get("values") or {}, account, to, grid=op.get("grid")))
        if not msgs:
            raise ValueError("no forms selected")
        res = winlink.put_in_outbox(install, account, msgs)
        print(f"{datetime.now():%H:%M:%S} winlink: {len(msgs)} message(s) to {', '.join(to)} in {install}\\{account} Outbox",
              flush=True)
        return res

    def _apply_operator(self):
        saved = self._saved()
        if (saved.get("operator") or {}).get("callsign"):
            self._apply_callsign(saved["operator"]["callsign"])
        if not saved:
            return
        for rid, s in (saved.get("radios") or {}).items():
            if rid in self.radios:
                self._set_tuning(rid, s.get("freq_mhz"), s.get("mode"), s.get("set_at"))

    def _set_tuning(self, rid, freq, mode, at):
        r = self.radios[rid]
        r.update(freq_mhz=freq, band=band(freq), mode=mode or r["mode"], tuned_at=at)
        for c in self.cfg["radios"]:
            if c["id"] == rid:
                c["freq_mhz"], c["mode"] = freq, r["mode"]

    def set_radio(self, rid: str, freq_mhz, mode: str | None = None) -> dict:
        """The operator says what the radio is tuned to. Validated, saved to data/operator.json
        (survives restarts and updates), shown everywhere, and stamped on every later transmission."""
        if rid not in self.radios:
            raise KeyError(rid)
        f = round(float(freq_mhz), 4)
        if not 0.1 <= f <= 3000:
            raise ValueError("frequency must be between 0.1 and 3000 MHz")
        m = (mode or self.radios[rid]["mode"] or "FM").upper()
        if m not in self.MODES:
            raise ValueError(f"mode must be one of {', '.join(self.MODES)}")
        at = self.clock().isoformat(timespec="seconds")
        with self.lock:
            self._set_tuning(rid, f, m, at)
            saved = self._saved()
            saved.setdefault("radios", {})[rid] = {"freq_mhz": f, "mode": m, "set_at": at}
            self._save(saved)
            radio = dict(self.radios[rid])
        self.bus.publish("radios", {"radios": list(self.radios.values()), "net": self.schedule_now()})
        return radio

    # ---------- schedule ----------
    def schedule_now(self, at: datetime | None = None) -> dict:
        at = at or self.clock()
        cur, nxt = None, None
        for n in self.cfg.get("schedule", []):
            days = [DAYS.index(d[:3].title()) for d in n.get("days", DAYS)]
            hh, mm = map(int, str(n.get("start", "00:00")).split(":"))
            for add in range(0, 8):
                d = (at + timedelta(days=add - 1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if d.weekday() not in days:
                    continue
                end = d + timedelta(minutes=int(n.get("duration_min", 60)))
                radio = self.radios.get(n.get("radio")) or {}
                f = n.get("freq_mhz") or radio.get("freq_mhz")
                item = {"name": n.get("name"), "radio": n.get("radio"), "radio_name": radio.get("name"),
                        "freq_mhz": f, "band": band(f), "ncs": n.get("ncs"), "start": d.isoformat(timespec="minutes"),
                        "end": end.isoformat(timespec="minutes"),
                        "retune": bool(f and radio.get("freq_mhz") and abs(f - radio["freq_mhz"]) > 0.0005)}
                if d <= at < end:
                    cur = cur or item
                elif d > at and (nxt is None or d.isoformat() < nxt["start"]):
                    nxt = item
        return {"current": cur, "next": nxt, "by_radio": self._current_by_radio(at)}

    def _current_by_radio(self, at) -> dict:
        out = {}
        for n in self.cfg.get("schedule", []):
            days = [DAYS.index(d[:3].title()) for d in n.get("days", DAYS)]
            hh, mm = map(int, str(n.get("start", "00:00")).split(":"))
            for add in (0, 1):
                d = (at - timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if d.weekday() in days and d <= at < d + timedelta(minutes=int(n.get("duration_min", 60))):
                    out.setdefault(n.get("radio"), {"name": n.get("name"), "ncs": n.get("ncs")})
        return out

    # ---------- radios ----------
    def _gate(self, r):
        if r["gate"] == "carrier":
            from capture.gates import CarrierGate
            return CarrierGate(r.get("carrier", {}), SR)
        from capture.recorder import SileroGate          # needs torch; HF/SSB radios
        return SileroGate({"threshold": r.get("vad_threshold", 0.5)})

    def segmenter(self, r) -> Segmenter:
        return Segmenter(self._gate(r), SR, 32, r.get("min_speech_ms", 400), r.get("hangover_ms", 900),
                         r.get("max_tx_s", 180), r.get("preroll_ms", 250), r.get("partial_every_s", 1.0))

    def _set_radio(self, rid, **kw):
        with self.lock:
            self.radios[rid].update(kw)

    @staticmethod
    def make_meter(r) -> Meter:
        return Meter(r.get("meter"), SR, r.get("gate", "carrier"), r.get("waterfall_hz"))

    def _source(self, r):
        """Context manager yielding a frame queue: a Windows sound input, or `file:<wav>` (played in real
        time and looped — demos, tests, and running several streams without radios)."""
        import contextlib
        dev = r.get("device") or ""

        @contextlib.contextmanager
        def from_file(path):
            audio, sr = sf.read(path, dtype="float32")
            audio = audio.mean(axis=1) if audio.ndim > 1 else audio
            if sr != SR:
                raise RuntimeError(f"{path}: needs {SR} Hz audio, got {sr}")
            frames, stop = queue.Queue(maxsize=200), threading.Event()

            def feed():
                t0, i = time.monotonic(), 0
                while not (stop.is_set() or self.stop.is_set()):
                    k = (i * FRAME) % max(len(audio) - FRAME, 1)
                    frames.put(audio[k:k + FRAME].copy()); i += 1
                    time.sleep(max(0.0, t0 + i * FRAME / SR - time.monotonic()))
            threading.Thread(target=feed, daemon=True).start()
            try:
                yield frames
            finally:
                stop.set()

        @contextlib.contextmanager
        def from_device(name):
            import sounddevice as sd
            idx = next((i for i, d in enumerate(sd.query_devices())
                        if name.lower() in d["name"].lower() and d["max_input_channels"] > 0), None)
            if idx is None:
                raise RuntimeError(f"audio input containing '{name}' not found")
            frames = queue.Queue(maxsize=400)

            def cb(d, n, t, s):          # never raise in the audio callback: it would stop the stream
                try:
                    frames.put_nowait(d[:, 0].copy())
                except queue.Full:
                    pass
            with sd.InputStream(device=idx, samplerate=SR, channels=1, dtype="float32", blocksize=FRAME,
                                callback=cb):
                yield frames
        return from_file(dev[5:]) if dev.startswith("file:") else from_device(dev)

    def _listen(self, r):
        rid = r["id"]
        while not self.stop.is_set():
            try:
                seg, meter = self.segmenter(r), self.make_meter(r)
                raw = RawRing(self.data / "raw" / rid, int(r.get("raw_keep_min", self.cfg.get("raw_keep_min", 60))))
                with self._source(r) as frames:
                    self._set_radio(rid, state="listening", error=None)
                    last = 0.0
                    while not self.stop.is_set():
                        frame = frames.get(timeout=5)
                        raw.add(frame)
                        self.handle(rid, seg, frame)
                        m = meter.feed(frame, seg.active, seg.gate)
                        if m:
                            m["id"] = rid
                            self.bus.publish("m", m)
                        if time.time() - last > 0.25:
                            last = time.time()
                            self._set_radio(rid, level_db=round(seg.level_db, 1), carrier=seg.active,
                                            snr_db=m["snr"] if m else self.radios[rid].get("snr_db"))
            except Exception as e:  # noqa: BLE001 — keep trying; a radio unplugged is not a crash
                self._set_radio(rid, state="error", error=f"{type(e).__name__}: {e}", carrier=False)
                self.stop.wait(10)

    def handle(self, rid, seg: Segmenter, frame: np.ndarray):
        for ev in seg.feed(frame):
            if ev[0] == "start":
                g = seg.gate
                print(f"{datetime.now():%H:%M:%S} {rid} gate OPEN"
                      + (f" (hiss floor {g.floor:.1f} dB, now {g.last_db:.1f})" if getattr(g, "floor", None) is not None else ""),
                      flush=True)
                self._tx_start(rid, ev[1])
            elif ev[0] == "audio":
                self.partial_slot[rid] = (self._live_id(rid), ev[1]); self.partial_ev.set()
            elif ev[0] == "end":
                i = ev[2]
                print(f"{datetime.now():%H:%M:%S} {rid} gate CLOSE {i['duration_s']}s"
                      + (" -> dropped (shorter than min_speech_ms)" if i["kerchunk"] else ""), flush=True)
                self._tx_end(rid, ev[1], ev[2])

    def _live_id(self, rid):
        return self.radios[rid].get("live_tx")

    def _tx_start(self, rid, t0):
        with self.lock:
            self.next_id += 1
            tid = str(self.next_id)
            self.radios[rid]["live_tx"] = tid
            r = self.radios[rid]
            tx = {"id": tid, "radio": rid, "radio_name": r["name"], "freq_mhz": r["freq_mhz"],
                  "start": datetime.fromtimestamp(t0).isoformat(timespec="seconds"), "status": "live",
                  "partial": "", "text": "", "callsigns": [], "speaker": None, "terms": []}
            self.tx.append(tx)
            del self.tx[:-500]
        self.bus.publish("tx", tx)

    def _tx_end(self, rid, audio, info):
        tid = self._live_id(rid)
        with self.lock:
            self.radios[rid]["live_tx"] = None
            self.partial_slot.pop(rid, None)
            tx = self._find(tid)
        if tx is None:
            return
        if info["kerchunk"]:
            with self.lock:
                self.tx.remove(tx)
            self.bus.publish("tx_drop", {"id": tid})
            return
        day = self.clock().strftime("%Y%m%d")
        wav = self.data / "recordings" / day / f"{tid}_{rid}.wav"
        wav.parent.mkdir(parents=True, exist_ok=True)
        sf.write(wav, audio, SR, subtype="PCM_16")
        tx.update(status="transcribing", duration_s=info["duration_s"], wav=str(wav.relative_to(self.data)))
        self.bus.publish("tx", tx)
        self.final_q.put((tid, audio))

    def inject(self, rid: str, audio: np.ndarray, realtime: bool = False):
        """Feed a recorded transmission as if it had just been heard (tests, --simulate)."""
        self._tx_start(rid, time.time())
        step = SR
        for i in range(step, len(audio), step):
            if realtime:
                time.sleep(1.0)
            self.partial_slot[rid] = (self._live_id(rid), audio[:i]); self.partial_ev.set()
        self._tx_end(rid, audio, {"kerchunk": False, "duration_s": round(len(audio) / SR, 2)})

    def _find(self, tid):
        return next((t for t in reversed(self.tx) if t["id"] == tid), None)

    def prompt_for(self, rid: str | None) -> str:
        """Short on purpose (see engine.make): frequency + the operator's call, spelled phonetically.
        Measured on W9XYZ-5: fixes 'This is a 79' -> 'This is W9XYZ' with no loops."""
        r = self.radios.get(rid) or {}
        parts = [f"Ham radio net on {r['freq_mhz']:.3f} MHz." if r.get("freq_mhz") else "Ham radio net."]
        if self.me:
            parts.append(f"This is {self.me}, {phonetic(self.me)}. Over.")
        return " ".join(parts)

    # ---------- workers ----------
    @staticmethod
    def _warm(eng):
        """The first GPU run compiles kernels (+1-2 s on W9XYZ-5); pay that at startup, not on air."""
        try:
            if getattr(eng, "warm", True):
                eng.transcribe(np.zeros(SR * 2, np.float32))
        except Exception:  # noqa: BLE001
            pass

    def _partial_worker(self):
        try:
            eng = self._partial_factory()
            self._warm(eng)
        except Exception as e:  # noqa: BLE001
            self.engines["partial_error"] = str(e); return
        self.engines["partial"] = eng.name
        while not self.stop.is_set():
            if not self.partial_ev.wait(0.5):
                continue
            self.partial_ev.clear()
            for rid, (tid, audio) in list(self.partial_slot.items()):
                if self.partial_slot.get(rid, (None,))[0] != tid:
                    continue
                text = eng.transcribe(audio[-30 * SR:], prompt=self.prompt_for(rid))["text"]
                with self.lock:
                    tx = self._find(tid)
                    if tx and tx["status"] == "live":
                        tx["partial"] = text
                        self.bus.publish("tx", tx)

    def _final_worker(self):
        try:
            eng = self._final_factory()
            self._warm(eng)
            self.engines.update(final=eng.name, status="ready")
        except Exception as e:  # noqa: BLE001
            self.engines.update(status="error", final_error=f"{type(e).__name__}: {e}")
            if not self._partial_factory:
                return
            eng = self._partial_factory()               # degrade: partial model makes the finals
            self.engines.update(final=eng.name + " (fallback)", status="degraded")
        self.bus.publish("engines", self.engines)
        while not self.stop.is_set():
            try:
                tid, audio = self.final_q.get(timeout=0.5)
            except queue.Empty:
                continue
            t = time.time()
            tx = self._find(tid)
            res = eng.transcribe(audio, prompt=self.prompt_for(tx and tx["radio"]))
            self.finish(tid, res, round(time.time() - t, 2))

    # ---------- enrichment ----------
    def finish(self, tid: str, res: dict, took_s: float = 0.0):
        validate = self.callbook.validate if self.callbook else None
        text, calls = normalize(res["text"], roster=self.roster, validate=validate)
        terms = [{k: v for k, v in g.items() if k != "pos"} for g in glossary.find(text)]
        with self.lock:
            tx = self._find(tid)
            if tx is None:
                return
            rid = tx["radio"]
            net = self._current_by_radio(self.clock()).get(rid) or {}
            at = datetime.fromisoformat(tx["start"])
            hinter = self.hinters.setdefault(rid, Hinter(self.me))
            hs = self.hints.setdefault(rid, {"cards": [], "reminders": [], "net": {}})
            h = hinter.on_transmission(text, calls, at=at, ncs=net.get("ncs"))
            if h["cards"]:
                hs["cards"] = h["cards"]
            hs.update(reminders=h["reminders"], net=h["net"])
            tx.update(status="final", text=text, callsigns=calls, speaker=speaker_of(text, calls), terms=terms,
                      model=res.get("model"), looped=res.get("looped", False), took_s=took_s,
                      hint_keys=[c["key"] for c in h["cards"]], partial="")
            new = []
            for c in calls:
                e = self.heard.setdefault(c, {"call": c, "count": 0, "first": tx["start"], "card": None})
                e["count"] += 1; e["last"] = tx["start"]
                if e["card"] is None:
                    e["card"] = self.callbook.card(c) if self.callbook else {"call": c, "sources": []}
                    new.append(c)
        self._log(tx)
        self.bus.publish("tx", tx)
        self.bus.publish("hints", self.hints)
        self.bus.publish("heard", {"heard": self.heard_list()})
        if new and self.callbook and self.callbook.qrz and self.cfg.get("qrz_online", True):
            threading.Thread(target=self._qrz_refresh, args=(new,), daemon=True).start()

    def _qrz_refresh(self, calls):
        for c in calls:
            card = self.callbook.card(c, qrz_network=True)
            with self.lock:
                if c in self.heard:
                    self.heard[c]["card"] = card
            self.bus.publish("heard", {"heard": self.heard_list()})

    def heard_list(self):
        with self.lock:
            return sorted(self.heard.values(), key=lambda e: e.get("last", ""), reverse=True)

    # ---------- persistence ----------
    def _log_path(self, day: datetime | None = None) -> Path:
        return self.data / "logs" / f"{(day or self.clock()).strftime('%Y%m%d')}.jsonl"

    def _log(self, tx):
        p = self._log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(tx) + "\n")

    def load_today(self):
        p = self._log_path()
        if not p.exists():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                tx = json.loads(line)
            except ValueError:
                continue
            self.tx.append(tx)
            for c in tx.get("callsigns", []):
                e = self.heard.setdefault(c, {"call": c, "count": 0, "first": tx["start"], "card": None})
                e["count"] += 1; e["last"] = tx["start"]
            self.hinters.setdefault(tx.get("radio"), Hinter(self.me)).on_transmission(
                tx.get("text", ""), tx.get("callsigns", []), at=datetime.fromisoformat(tx["start"]))
        for c, e in self.heard.items():
            e["card"] = self.callbook.card(c) if self.callbook else {"call": c, "sources": []}
        for rid, h in self.hinters.items():
            self.hints.setdefault(rid, {"cards": [], "reminders": [], "net": {}})["net"] = h.classify()
        del self.tx[:-500]
        self.next_id = max([self.next_id] + [int(t["id"]) for t in self.tx if str(t["id"]).isdigit()])

    def snapshot(self) -> dict:
        with self.lock:
            return {"callsign": self.me, "radios": list(self.radios.values()), "tx": self.tx[-200:],
                    "heard": self.heard_list(), "hints": self.hints, "net": self.schedule_now(),
                    "schedule": self.cfg.get("schedule", []), "engines": self.engines}
