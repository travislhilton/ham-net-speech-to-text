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
from hamkit.callsigns import load_roster, normalize, _ANY as CALL_SHAPE
from .fit import infer_model as fit_infer
from hamkit.hints import Hinter, phonetic
from . import callfix
from . import speaker
from .meter import Meter
from .segmenter import Segmenter

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HAMNET_DATA", ROOT / "data"))
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SR = 16000


def own_call_in(text: str, me: str) -> bool:
    return bool(me) and me in re.sub(r"[^A-Z0-9 ]", " ", (text or "").upper()).split()


def own_call_shown(text: str, me: str) -> bool:
    """Signs of the operator's call in a decode made without it in the prompt: the call within one character
    ("W9XYH"), or its prefix spoken phonetically ("Whiskey Niner")."""
    t = (text or "").upper()
    near = any(len(w) == len(me) and sum(a != b for a, b in zip(w, me)) <= 1
               for w in re.sub(r"[^A-Z0-9 ]", " ", t).split())
    from hamkit.callsigns import PHON
    first = next((k for k, v in PHON.items() if v == me[0] and k.isalpha()), "")
    digit = re.search(r"\d", me)
    spoken = bool(first and digit and re.search(r"\b" + first.upper() + r"\W+(" + digit.group() + "|" +
                                                "|".join(k.upper() for k, v in PHON.items() if v == digit.group()) + r")\b", t))
    return near or spoken
FRAME = 512                       # 32 ms @ 16 kHz


def load_config(path: Path = ROOT / "config" / "station.yaml") -> dict:
    # utf-8-sig: Windows PowerShell 5 (`Set-Content -Encoding utf8`, Notepad of old) writes a byte-order mark, and
    # with it the first line is not a comment and the file fails to parse (a laptop station install, 2026-11-04).
    c = yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
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


# What Whisper says when it hears only noise (seen on the 145.900 net 2026-11-02: "So.", "The end of the day.",
# "We'll be right back."). Such a transmission is kept and playable, but marked as probable noise.
NOISE_PHRASES = {"so", "the end of the day", "the end of the day is a great day", "well be right back",
                 "thanks for watching", "thank you for watching", "bye", "you", "okay", "oh", "uh", "um", "hmm",
                 "see you later on in the morning", "please subscribe", ""}


def probably_noise(text: str, calls: list[str]) -> bool:
    words = re.sub(r"[^a-z ]", "", (text or "").lower().replace("'", "")).split()
    return not calls and (" ".join(words) in NOISE_PHRASES or len(words) <= 1)


def speaker_of(text: str, calls: list[str]) -> str | None:
    """The callsign the transmission identifies as its own (station/speaker.py), else None — never a guess."""
    return speaker.stated(text, calls)


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
        self.root = Path(__file__).resolve().parent.parent
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
        import collections
        self.level_recent = {r["id"]: collections.deque(maxlen=150) for r in config["radios"]}   # Set level… (15 s)
        self.level_tap: dict = {}                       # rid -> list of raw frames while a knob position is measured
        self.knob_points: dict = {r["id"]: {} for r in config["radios"]}
        from .silence import SilenceWatch                # a radio switched off / asleep / unplugged (station/silence.py)
        from .voicehint import VoiceHints
        self.voice = VoiceHints(self.data, enabled=bool(config.get("voice_hints", True)))
        self.silence = {r["id"]: SilenceWatch(r.get("gate", "carrier"), r.get("silence")) for r in config["radios"]}
        from .morse import MorseWatch                     # Morse code (CW) on any radio (station/morse.py)
        self.morse = {r["id"]: MorseWatch() for r in config["radios"]
                      if r.get("morse", config.get("morse", True)) is not False}
        self.radio_cfg = {r["id"]: r for r in config["radios"]}
        self.radios = {r["id"]: {"id": r["id"], "name": r.get("name", r["id"]), "device": r.get("device"),
                                 "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode", "FM"),
                                 "band": band(r.get("freq_mhz")), "gate": r["gate"], "state": "starting",
                                 "level_db": None, "carrier": False, "error": None,
                                 "meter": self.meters[r["id"]].limits(),
                                 # what it can hear (station/fit.py): model from Radios…, antenna/role on its card
                                 "model": r.get("model") or fit_infer(r), "role": "nets",
                                 "antenna": "hf" if (r.get("model") or fit_infer(r)) == "ic705" else None}
                       for r in config["radios"]}
        self._apply_operator()
        from .schedule import NetSchedule      # schedule: dashboard-edited nets + listen only during nets + alerts
        self.nets = NetSchedule(self)
        from .history import History            # net history, search, analysis, minutes (data/history.db)
        self.history = History(self.data, nets=lambda: self.cfg.get("schedule") or [], radios=lambda: self.radios,
                               callbook=callbook, clock=clock, me=self.me, bus=self.bus)
        from .netclose import NetCloser         # the end of a net: net control closing it, or End net
        self.closer = NetCloser(self, config)
        from .awake import KeepAwake, MODES as AWAKE_MODES, DEFAULT_MODE as AWAKE_DEFAULT   # full speed while transcribing (laptops throttle when dark)
        self.awake = KeepAwake(snapshot=self.data / "power_snapshot.json")
        self.awake_mode = config.get("keep_awake", AWAKE_DEFAULT)
        self.awake_quiet_min = float(config.get("keep_awake_quiet_min", 10))
        self.last_traffic = None                       # time.time() of the last transmission's end (keep awake)
        self.viewers, self.viewer_seen = 0, None       # dashboards open in a browser (server.py /api/events)
        self._canary = None                            # (audio, words, prompt): a recent final that read well
        self._engine_check = False                     # a dashboard just opened: check the live engine when idle
        self._engine_reload_at = 0.0
        self._partial_reload = False
        if self.awake_mode not in AWAKE_MODES:
            self.awake_mode = AWAKE_DEFAULT
        self.awake_mode = (self._saved().get("operator") or {}).get("keep_awake") or self.awake_mode
        self.final_q: queue.Queue = queue.Queue()
        self.partial_slot: dict[str, tuple] = {}
        self.partial_ev = threading.Event()
        self._final_factory, self._partial_factory = final_engine_factory, partial_engine_factory
        self.engines = {"final": None, "partial": None, "status": "loading"}
        self._final_busy = False
        self.accurate_spec = None          # engine.yaml `accurate:` (set by __main__): what this PC can run locally
        self.stop = threading.Event()
        self.next_id = int(time.time() * 1000)

    # ---------- lifecycle ----------
    def start(self, listen=True):
        self.load_today()
        for t in list(self.tx):                                  # today's voiceprints, in the background
            self.voice.queue(t)
        threading.Thread(target=self._index_history, daemon=True, name="history").start()
        threading.Thread(target=self._final_worker, daemon=True, name="final").start()
        if self._partial_factory:
            threading.Thread(target=self._partial_worker, daemon=True, name="partial").start()
        if listen:
            for r in self.cfg["radios"]:
                threading.Thread(target=self._listen, args=(r,), daemon=True, name=f"radio-{r['id']}").start()
        from .cat import CatControl                   # CAT: read and tune radios that have a CI-V port
        self.cat = CatControl(self, getattr(self, "_cat_factory", None))
        if self.cat.cfg:
            threading.Thread(target=self.cat.run, args=(self.stop,), daemon=True, name="cat").start()
        threading.Thread(target=self._ticker, daemon=True, name="ticker").start()
        from .accurate import AccuratePass
        from . import accurate_local
        local = getattr(self, "_accurate_local", None) or accurate_local.make(self, self.cfg.get("accurate"),
                                                                               self.accurate_spec)
        self.accurate = AccuratePass(self, self.cfg.get("accurate"), getattr(self, "_accurate_remote", None), local)
        threading.Thread(target=self.accurate.run, args=(self.stop,), daemon=True, name="accurate").start()

    def _index_history(self):
        """Bring the history index up to date with the day logs (the first run: all of them), in the background."""
        try:
            t = time.time()
            n = self.history.catch_up(self.data / "logs")
            if n:
                print(f"history: {n} log lines indexed in {time.time() - t:.1f} s", flush=True)
            self.history.flush()
        except Exception as e:  # noqa: BLE001 — the history never stops the station
            print(f"history: {type(e).__name__}: {e}", flush=True)

    def _ticker(self):
        n = 0
        while not self.stop.wait(1.0):
            n += 1
            if n % 10 == 0:
                try:
                    self.history.flush()                 # participants, counts, minutes drafts of changed nets
                except Exception as e:  # noqa: BLE001
                    print(f"history: {type(e).__name__}: {e}", flush=True)
            if n % 60 == 0:
                try:
                    self.refix_ended_nets()
                except Exception as e:  # noqa: BLE001
                    print(f"refix: {type(e).__name__}: {e}", flush=True)
            if n % 60 == 30:                             # net-wide callsign agreement, in its own thread
                self.agree_recent_async()
            if n % 15 == 0 and getattr(self, "accurate", None):
                try:
                    self.accurate.tick()
                except Exception as e:  # noqa: BLE001 — never let the pass stop the station
                    print(f"accurate tick: {type(e).__name__}: {e}", flush=True)
            try:                               # a closed net's tail: pause, final pass, minutes
                self.closer.tick()
            except Exception as e:  # noqa: BLE001
                print(f"netclose tick: {type(e).__name__}: {e}", flush=True)
            try:                               # schedule: resume/pause radios at their nets
                self.nets.tick()
            except Exception as e:  # noqa: BLE001 — never let the schedule stop the station
                print(f"schedule tick: {type(e).__name__}: {e}", flush=True)
            if n % 5 == 1:                     # hold the screen on (full speed) only while transcribing
                self._keep_awake_tick()
            if n % 10 == 3:                    # a radio switched off / asleep / unplugged
                self._silence_tick()
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

    def _silence_tick(self):
        """Warn on the radio card (and in station.log) when a radio went silent; clear it when sound comes back."""
        try:
            by = self.schedule_now().get("by_radio") or {}
        except Exception:  # noqa: BLE001
            by = {}
        changed = False
        for rid, w in self.silence.items():
            r = self.radios[rid]
            s = None if r.get("paused") or r.get("state") != "listening" else w.state(time.time(), bool(by.get(rid)))
            old = r.get("silent")
            if (old or {}).get("kind") != (s or {}).get("kind"):
                self._set_radio(rid, silent=s)
                changed = True
                print(f"{datetime.now():%H:%M:%S} {rid} " + (f"SILENT ({s['kind']}): {s['say']}" if s else "sound is back"),
                      flush=True)
        if changed:
            self.bus.publish("radios", {"radios": list(self.radios.values()), "net": self.schedule_now()})

    def _keep_awake_tick(self):
        from .awake import wanted
        lk = self.__dict__.setdefault("_awake_lock", threading.Lock())
        with lk:
            self._keep_awake_once(wanted)

    def _keep_awake_once(self, wanted):
        try:
            with self.lock:
                radios = [dict(r, id=k) for k, r in self.radios.items()]
            acc = getattr(self, "accurate", None)
            busy = self.live_busy()
            if busy:
                self.last_traffic = time.time()
            try:
                net = (self.schedule_now().get("current") or {}).get("name")
            except Exception:  # noqa: BLE001
                net = None
            want, why = wanted(self.awake_mode, radios, acc.status() if acc else [], busy=busy,
                               quiet_s=None if self.last_traffic is None else time.time() - self.last_traffic,
                               net_on=net, quiet_min=self.awake_quiet_min, viewer=self.dashboard_open())
            if self.awake.set(want):
                print(f"{datetime.now():%H:%M:%S} keep awake: {'on' if want else 'off'} ({why})", flush=True)
        except Exception as e:  # noqa: BLE001 — power management never stops the station
            print(f"keep awake: {type(e).__name__}: {e}", flush=True)

    # ---------- operator settings (set on the dashboard; the operator tunes the radio) ----------
    KEEP_TX = 6000      # in memory (the day's log keeps all): a busy day on two radios is ~1000; the Winlink span,
                        # the accurate pass and the dashboard all need a net's FIRST transmission
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
        op = {"callsign": self.me, "name": "", "grid": "", "winlink_dir": "", "winlink_account": "",
              "reach_km": self.reach_km(), "keep_awake": self.awake_mode} | \
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

    def reach_km(self) -> int:
        """VHF/UHF: beyond this a call said once that nothing backs is not counted (Settings; consensus.REACH_KM)."""
        from .consensus import REACH_KM
        try:
            v = int((self._saved().get("operator") or {}).get("reach_km") or REACH_KM)
        except (TypeError, ValueError):
            v = REACH_KM
        return v

    def set_operator(self, body: dict) -> dict:
        """The operator's own settings, set on the dashboard: callsign (hints, IDs, Winlink sender), name,
        grid, which Winlink Express install and which callsign account in it."""
        call = str(body.get("callsign", self.me) or "").upper().strip()
        if call and not re.fullmatch(r"[A-Z0-9]{3,8}(-\d{1,2})?", call):
            raise ValueError("callsign looks wrong")
        grid = str(body.get("grid", "") or "").strip()
        if grid and not re.fullmatch(r"[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?", grid):
            raise ValueError("grid square must look like EM59 or EM59et")
        from .awake import MODES as AWAKE_MODES
        ka = body.get("keep_awake")
        if ka not in (None, "") and ka not in AWAKE_MODES:
            raise ValueError("keep awake must be one of " + ", ".join(AWAKE_MODES))
        reach = body.get("reach_km")
        if reach not in (None, ""):
            try:
                reach = int(float(reach))
            except (TypeError, ValueError):
                raise ValueError("VHF/UHF reach must be a number of km")
            if not 50 <= reach <= 5000:
                raise ValueError("VHF/UHF reach must be between 50 and 5000 km")
        with self.lock:
            saved = self._saved()
            op = saved.get("operator") or {}
            if reach not in (None, ""):
                if reach != op.get("reach_km"):
                    self.__dict__.pop("_agreed", None)          # re-run today's agreement with the new reach
                    threading.Timer(0.5, self.agree_recent_async).start()
                op["reach_km"] = reach
            op.update(callsign=call, name=str(body.get("name", op.get("name", "")))[:80], grid=grid,
                      winlink_dir=str(body.get("winlink_dir", op.get("winlink_dir", "")))[:260],
                      winlink_account=str(body.get("winlink_account", op.get("winlink_account", ""))).upper()[:12])
            if ka:
                op["keep_awake"] = ka
                self.awake_mode = ka
            saved["operator"] = op
            self._save(saved)
            self._apply_callsign(call)
        if ka:
            threading.Thread(target=self._keep_awake_tick, daemon=True).start()
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
        key = self.history.instance_of(rid, start)
        inst, notes = None, ""
        if key:
            try:
                inst = self.history.instance(key)
                notes = self.history.summary_for_213(key)
            except LookupError:
                pass
        name = title or sched.get("name") or (inst or {}).get("name") if (inst or {}).get("kind") == "net" \
            else title or sched.get("name")
        d = ics.drafts(span, self.radios[rid], op, people, net.get("type"), name, minutes=notes)
        acc = None
        if getattr(self, "accurate", None):
            jobs = [j for j in self.accurate.status() if j["radio"] == rid and start <= j["start"] <= end]
            acc = next((j for j in jobs if j["state"] in ("queued", "running")), None) or \
                next((j for j in jobs if j.get("stage") == "final"), None) or (jobs[0] if jobs else None)
        from .accurate import stage_of
        with self.lock:
            # waiting = not yet final (a provisional text is replaced when the net's final pass lands)
            waiting = sum(1 for t in ics.select(list(self.tx), rid, start, end, include_noise=True)
                          if t.get("wav") and stage_of(t) != "final")
        return {"radio": rid, "start": start, "end": end, "count": len(span), "to": people, "drafts": d, "inst": key,
                "accurate": acc, "accurate_missing": waiting,
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
        if body.get("radio") and body.get("start"):
            try:
                self.history.note_winlink(body["radio"], body["start"], [d.get("form") for d in body.get("drafts", [])], to)
            except Exception as e:  # noqa: BLE001
                print(f"history: {type(e).__name__}: {e}", flush=True)
        print(f"{datetime.now():%H:%M:%S} winlink: {len(msgs)} message(s) to {', '.join(to)} in {install}\\{account} Outbox",
              flush=True)
        return res

    # ---------- Radios…: add / replace / remove (station/radios.py) ----------
    def radio_list(self) -> dict:
        from . import radios as rmod
        with self.lock:
            cur = [{k: self.radio_cfg.get(r, {}).get(k) for k in rmod.EDITABLE}
                   | {"state": self.radios[r].get("state"), "error": self.radios[r].get("error")} for r in self.radios]
        nets = {}
        for n in self.cfg.get("schedule", []):
            if n.get("radio"):
                nets.setdefault(n["radio"], []).append(n.get("name"))
        return {"radios": cur, "inputs": rmod.fresh_inputs() or rmod.inputs(), "nets": nets, "ports": rmod.serial_ports(),
                "from": "dashboard" if (self.data / "radios.json").exists() else "station.yaml",
                "restart": os.name == "nt"}

    def save_radios(self, body: dict) -> dict:
        from . import radios as rmod
        new = rmod.clean(body.get("radios") or [])
        if not new:
            raise ValueError("keep at least one radio")
        gone = {n.get("radio") for n in self.cfg.get("schedule", [])} - {r["id"] for r in new} - {None}
        rmod.save(self.data / "radios.json", new)
        if gone:                    # its nets stay in the list with no radio (Nets: "Needs you", a radio suggested)
            self.nets.unassign(gone)
        print(f"{datetime.now():%H:%M:%S} radios saved on the dashboard: " +
              "; ".join(f"{r['id']}={r['name']} @ {r['device']}" for r in new), flush=True)
        self.restart_soon()
        return {"ok": True, "radios": new, "restarting": os.name == "nt"}

    def restart_soon(self):
        """Apply a radio change: on Windows end this process and ask Task Scheduler to start the station again (its
        2-minute watchdog would anyway). Elsewhere (development) the operator restarts it."""
        if os.name != "nt" or getattr(self, "no_restart", False):
            return
        import subprocess

        def go():
            self.bus.publish("restarting", {"in_s": 5})
            subprocess.Popen('cmd /c "ping -n 5 127.0.0.1 >nul & schtasks /run /tn "HamNet Station""',   # no console:
                             creationflags=0x00000008 | 0x08000000)          # DETACHED_PROCESS | CREATE_NO_WINDOW
            self.stop.set()
            time.sleep(1.5)
            os._exit(0)
        threading.Timer(0.5, go).start()

    def _level_ctx(self, rid: str):
        from . import levels
        if rid not in self.radios:
            raise KeyError(rid)
        r = self.radios[rid]
        band = levels.band_for(r.get("mode"), r.get("gate"))
        return levels, r, band, levels.WinGain(r.get("device") or "")

    def level(self, rid: str, step: str = "noise") -> dict:
        """Set level… : the last 3 s against the target for this step, and the Windows recording level."""
        levels, r, band, gain = self._level_ctx(rid)
        st = levels.stats(list(self.level_recent[rid]), time.time() - levels.WINDOW_S, voice=step == "voice")
        if step == "knob":
            pts = [self.knob_points[rid][k] for k in levels.KNOBS if k in self.knob_points[rid]]
            return {"radio": rid, "step": step, "state": r.get("state"), "knobs": levels.KNOBS,
                    "result": levels.best_knob(pts), "gain": gain.get()}
        return {"radio": rid, "step": step, "mode": r.get("mode"), "gate": r.get("gate"), "state": r.get("state"),
                "band": list(band) if step == "noise" else list(levels.VOICE_PEAK), "stats": st,
                "advice": levels.advise(step, st, band), "gain": gain.get()}

    def set_level(self, rid: str, body: dict) -> dict:
        """{"action": "auto"} brings the noise into the target with the Windows level; {"db": x} or {"step": ±1}
        sets it by hand. The radio must be listening (not paused)."""
        levels, r, band, gain = self._level_ctx(rid)
        if r.get("state") != "listening":
            raise RuntimeError("the radio is not listening: press Resume first")
        if gain.get() is None:
            raise RuntimeError("this PC's input volume can't be set from here: Windows Settings → System → Sound → "
                               "Input → this input → Input volume (and the radio's volume knob)")
        act = body.get("action")
        if act == "knob_reset":
            self.knob_points[rid] = {}
            return self.level(rid, "knob")
        if act == "knob":                               # {"knob": "1/4", "squelch": "open"|"closed"}
            k, sq = str(body.get("knob")), str(body.get("squelch"))
            if k not in levels.KNOBS or sq not in ("open", "closed"):
                raise ValueError("knob must be one of " + ", ".join(levels.KNOBS) + "; squelch open or closed")
            if sq == "open":                            # the Windows level into the band first: fair comparison
                levels.auto(lambda since: levels.stats(list(self.level_recent[rid]), since), gain, band)
            x = self._capture(rid, 3.0)
            a = levels.analyze(x, SR)
            p = self.knob_points[rid].setdefault(k, {"knob": k})
            p[sq] = a
            p[sq + "_gain_db"] = (gain.get() or {}).get("db")
            print(f"{datetime.now():%H:%M:%S} {rid} knob {k} squelch {sq}: {a}", flush=True)
            return self.level(rid, "knob")
        if act == "auto":
            res = levels.auto(lambda since: levels.stats(list(self.level_recent[rid]), since), gain, band)
            print(f"{datetime.now():%H:%M:%S} {rid} level set automatically: {res['advice']['say']} "
                  f"(Windows {res['gain']['db'] if res['gain'] else '?'} dB)", flush=True)
            return self.level(rid) | {"auto": res}
        if "db" in body:
            gain.set_db(float(body["db"]))
        elif "step" in body:
            g = gain.get()
            gain.set_db(g["db"] + max(-6.0, min(6.0, float(body["step"]))))
        return self.level(rid, str(body.get("for") or "noise"))

    def _capture(self, rid: str, seconds: float) -> np.ndarray:
        """The radio's raw audio for `seconds` (while it is listening)."""
        tap: list = []
        self.level_tap[rid] = tap
        try:
            t = time.time()
            while sum(len(f) for f in tap) < seconds * SR and time.time() - t < seconds + 5:
                time.sleep(0.1)
        finally:
            self.level_tap.pop(rid, None)
        if not tap:
            raise RuntimeError("no audio came from the radio's input")
        return np.concatenate(tap)

    def set_paused(self, rid: str, paused: bool, by: str = "operator") -> dict:
        """Stop (or resume) listening to a radio: the sound input is closed, nothing is recorded or
        transcribed until resumed. Kept across restarts. `by`: "operator" or "schedule" (shown on the card)."""
        if rid not in self.radios:
            raise KeyError(rid)
        with self.lock:
            self.radios[rid]["paused"] = bool(paused)
            self.radios[rid]["paused_by"] = by if paused else None
            if by == "operator":          # the operator's own Pause/Resume during a net wins over the net's end
                self.radios[rid].setdefault("listen", {}).pop("resumed_for", None)
                self.radios[rid]["listen"]["operator_at"] = self.clock().isoformat(timespec="seconds")
            if not paused:
                self.radios[rid].pop("closed", None)       # "Paused: <net> closed by net control" no longer applies
            if not paused and self.radios[rid].get("state") == "paused":
                self.radios[rid]["state"] = "starting"
            saved = self._saved()
            saved.setdefault("radios", {}).setdefault(rid, {}).update(paused=bool(paused),
                                                                     paused_by=by if paused else None,
                                                                     resumed_by=None if paused else by)
            self.radios[rid]["resumed_by"] = None if paused else by
            self._save(saved)
            radio = dict(self.radios[rid])
        print(f"{datetime.now():%H:%M:%S} {rid} {'PAUSED' if paused else 'RESUMED'} by the {by}", flush=True)
        self.bus.publish("radios", {"radios": list(self.radios.values()), "net": self.schedule_now()})
        return radio

    def clear_radio(self, rid: str) -> dict:
        """Clear a radio's log and stations heard from the dashboard. The day's log file and the recordings
        stay on disk (data/logs, data/recordings): this hides them from the screen, it deletes nothing."""
        if rid not in self.radios:
            raise KeyError(rid)
        at = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            live = self.radios[rid].get("live_tx")
            self.tx = [t for t in self.tx if t.get("radio") != rid or t.get("id") == live]
            self.radios[rid]["cleared_at"] = at
            self.hinters[rid] = Hinter(self.me)
            self.hints[rid] = {"cards": [], "reminders": [], "net": self.hinters[rid].classify()}
            self._rebuild_heard()
            saved = self._saved()
            saved.setdefault("radios", {}).setdefault(rid, {})["cleared_at"] = at
            self._save(saved)
        print(f"{datetime.now():%H:%M:%S} {rid} log CLEARED by the operator (files kept)", flush=True)
        self.bus.publish("snapshot", self.snapshot())
        return {"radio": rid, "cleared_at": at}

    def _rebuild_heard(self):
        """Stations heard = callsigns in the transmissions still on screen, after the net-wide agreement (cards
        kept, no new lookups)."""
        from .consensus import calls_of
        old, self.heard = self.heard, {}
        for tx in self.tx:
            if tx.get("status") != "final":
                continue
            for c in calls_of(tx):                       # corrected > agreed (accurate) > live
                e = self.heard.setdefault(c, {"call": c, "count": 0, "first": tx["start"],
                                              "card": (old.get(c) or {}).get("card")})
                e["count"] += 1; e["last"] = tx["start"]
        for c, e in self.heard.items():                  # the agreement's word: no callbook confirms it
            w = (self.__dict__.get("unconfirmed") or {}).get(c)
            if w:
                e["unconfirmed"] = w

    def _apply_operator(self):
        saved = self._saved()
        if (saved.get("operator") or {}).get("callsign"):
            self._apply_callsign(saved["operator"]["callsign"])
        if not saved:
            return
        for rid, s in (saved.get("radios") or {}).items():
            if rid in self.radios:
                if "freq_mhz" in s:
                    self._set_tuning(rid, s.get("freq_mhz"), s.get("mode"), s.get("set_at"))
                self.radios[rid]["paused"] = bool(s.get("paused"))
                if s.get("antenna") in ("hf", "vu") and self.radios[rid].get("antenna"):
                    self.radios[rid]["antenna"] = s["antenna"]
                if s.get("role") in ("nets", "winlink"):
                    self.radios[rid]["role"] = s["role"]
                self.radios[rid]["resumed_by"] = s.get("resumed_by")
                self.radios[rid]["cleared_at"] = s.get("cleared_at")

    def _set_tuning(self, rid, freq, mode, at):
        r = self.radios[rid]
        r.update(freq_mhz=freq, band=band(freq), mode=mode or r["mode"], tuned_at=at)
        for c in self.cfg["radios"]:
            if c["id"] == rid:
                c["freq_mhz"], c["mode"] = freq, r["mode"]

    def set_radio_setup(self, rid: str, body: dict) -> dict:
        """The card: which antenna a one-socket multi-band radio has on ("hf" | "vu"), and whether the radio is used
        for nets or kept for Winlink. Saved in data/operator.json; nets are re-checked at once (station/fit.py)."""
        if rid not in self.radios:
            raise KeyError(rid)
        r = self.radios[rid]
        with self.lock:
            saved = self._saved()
            s = saved.setdefault("radios", {}).setdefault(rid, {})
            if "antenna" in body:
                if not r.get("antenna"):
                    raise ValueError(f"{r['name']} has no antenna choice")
                if body["antenna"] not in ("hf", "vu"):
                    raise ValueError("antenna must be hf or vu")
                r["antenna"] = s["antenna"] = body["antenna"]
            if "role" in body:
                if body["role"] not in ("nets", "winlink"):
                    raise ValueError("role must be nets or winlink")
                r["role"] = s["role"] = body["role"]
            self._save(saved)
        print(f"{datetime.now():%H:%M:%S} {rid}: antenna {r.get('antenna')}, used for {r.get('role')}", flush=True)
        self.bus.publish("radios", {"radios": list(self.radios.values()), "net": self.schedule_now()})
        self.nets.publish()
        return dict(r)

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
            saved.setdefault("radios", {}).setdefault(rid, {}).update(freq_mhz=f, mode=m, set_at=at)
            self._save(saved)
            radio = dict(self.radios[rid])
        self.bus.publish("radios", {"radios": list(self.radios.values()), "net": self.schedule_now()})
        return radio

    # ---------- schedule ----------
    def schedule_now(self, at: datetime | None = None) -> dict:
        """The net on the air now (first to start) and the next one, any radio, for the header and the cards."""
        from . import recur
        from .schedule import item, running
        at = at or self.clock()
        nets = self.cfg.get("schedule", [])

        def deco(it):
            radio = self.radios.get(it.get("radio")) or {}
            f = it.get("freq_mhz") or radio.get("freq_mhz")
            return it | {"radio_name": radio.get("name"), "freq_mhz": f, "band": band(f),
                         "retune": bool(f and radio.get("freq_mhz") and abs(f - radio["freq_mhz"]) > 0.0005)}
        run = running(nets, at)
        for rid in list(self.radios):                         # nets still going past their scheduled end
            try:
                ov = self.overrun(rid, at)
            except Exception:  # noqa: BLE001
                ov = None
            if ov and not any(r["key"] == ov["key"] for r in run):
                run.append(ov)
        nxt, nxt_r = None, {}
        for n in nets:
            for o in recur.upcoming(n, at, 2):
                if o["start"] > at and (nxt is None or o["start"] < nxt[0]):
                    nxt = (o["start"], item(n, o))
                r = n.get("radio")
                if o["start"] > at and r and (r not in nxt_r or o["start"] < nxt_r[r][0]):
                    nxt_r[r] = (o["start"], item(n, o))
        return {"current": deco(run[0]) if run else None, "next": deco(nxt[1]) if nxt else None,
                "running": [deco(r) for r in run], "by_radio": self._current_by_radio(at),
                "next_by_radio": {r: deco(v[1]) for r, v in nxt_r.items()},     # the log's Up next, per radio
                "today": [deco(x) for x in self.today_nets(at)]}

    def today_nets(self, at: datetime | None = None) -> list[dict]:
        """The nets still to come today (and the ones on the air), in start order, each with a state:
        "on" (on the air), "next" (the first one still to start) or "later"; a net closed early or skipped
        today is left out once it is over. The header's Today strip."""
        from . import recur
        from .schedule import item
        at = at or self.clock()
        day0 = at.replace(hour=0, minute=0, second=0, microsecond=0)
        closer = getattr(self, "closer", None)
        try:
            skipped = set(self.nets.alerts.skipped()) if getattr(self, "nets", None) else set()
        except Exception:  # noqa: BLE001
            skipped = set()
        rows = []
        for n in self.cfg.get("schedule", []) or []:
            for o in recur.occurrences(n, day0, day0 + timedelta(days=1)):
                it = item(n, o)
                if o["start"].date() != at.date():
                    continue
                if o["end"] <= at:
                    ov = self.overrun(it["radio"], at) if it["radio"] in self.radios else None
                    if not ov or ov["key"] != it["key"]:
                        continue
                    it["overrun"] = True
                c = closer.closes.get(it["key"]) if closer and hasattr(closer, "closes") else None
                if c and c.get("state") == "closed":
                    continue
                it["state"] = "on" if o["start"] <= at else "later"
                it["skipped"] = it["key"] in skipped
                rows.append(it)
        rows.sort(key=lambda x: x["start"])
        for it in rows:
            if it["state"] == "later" and not it["skipped"]:
                it["state"] = "next"
                break
        return rows

    def _current_by_radio(self, at) -> dict:
        from .schedule import running
        out = {}
        for it in running(self.cfg.get("schedule", []), at):
            out.setdefault(it["radio"], {"name": it["name"], "ncs": it.get("ncs"), "key": it["key"], "id": it["id"]})
        return out

    def ended_nets(self, rid: str, at: datetime, gap_min: int = 15) -> set[str]:
        """Occurrence keys on `rid` that are over before `at` even if their scheduled time is not: closed early
        (station/netclose.py), or already heard and then quiet for more than `gap_min` minutes (the log's rule for a
        new conversation). A net heard nothing from yet is never "ended": its first transmission may be late."""
        out, last = set(), {}
        closer = getattr(self, "closer", None)
        for k, c in ((closer.closes if closer and hasattr(closer, "closes") else {}) or {}).items():
            if c.get("radio") == rid and c.get("state") == "closed":
                out.add(k)
        for t in self.tx:
            k = (t.get("net") or {}).get("key")
            if k and t.get("radio") == rid and t.get("start", "") < at.isoformat():
                last[k] = max(last.get(k, ""), t["start"])
        for k, s in last.items():
            if at - datetime.fromisoformat(s) > timedelta(minutes=gap_min):
                out.add(k)
        return out

    def last_heard(self, rid: str, at: datetime, within_min: int = 30) -> dict:
        """Occurrence key -> when it was last heard on `rid` (looking back `within_min` minutes from `at`)."""
        out, frm, to = {}, (at - timedelta(minutes=within_min)).isoformat(), at.isoformat()
        for t in reversed(self.tx):                           # newest last: stop once older than the window
            s = t.get("start", "")
            if s < frm:
                break
            k = (t.get("net") or {}).get("key")
            if k and t.get("radio") == rid and s <= to and k not in out:
                out[k] = datetime.fromisoformat(s)
        return out

    def overrun(self, rid: str, at: datetime | None = None) -> dict | None:
        """The net on `rid` running past its scheduled end (station/schedule.overrun), or None."""
        from .schedule import overrun
        at = at or self.clock()
        closer = getattr(self, "closer", None)
        closed = {k for k, c in ((closer.closes if closer and hasattr(closer, "closes") else {}) or {}).items()
                  if c.get("radio") == rid and c.get("state") == "closed"}
        return overrun(self.cfg.get("schedule") or [], rid, at, self.last_heard(rid, at), closed,
                       quiet=self.cfg.get("overrun_quiet_min", 5), most=self.cfg.get("overrun_max_min", 180))

    def net_of(self, rid: str, at: datetime, freq_mhz: float | None = None) -> dict | None:
        """The scheduled occurrence a transmission on `rid` at `at` (heard on `freq_mhz`) belongs to, or None.
        A net running past its end keeps its transmissions until another net on the radio is due."""
        from .schedule import occurrence_at
        ov = self.overrun(rid, at)
        if ov:
            return {"key": ov["key"], "id": ov["id"], "date": ov["date"], "name": ov["name"]}
        o = occurrence_at(self.cfg.get("schedule") or [], rid, at, freq_mhz=freq_mhz, ended=self.ended_nets(rid, at))
        return {"key": o["key"], "id": o["id"], "date": o["date"], "name": o["name"]} if o else None

    # ---------- radios ----------
    def _gate(self, r):
        if r["gate"] == "carrier":
            from capture.gates import CarrierGate
            return CarrierGate(r.get("carrier", {}), SR)
        from capture.gates import SileroGate             # needs torch; HF/SSB radios
        return SileroGate({"threshold": r.get("vad_threshold", 0.5)})

    # Segmenting defaults per gate (station/segmenter.py; README "Segmenting"). FM carrier: the repeater's hang
    # time keeps the carrier up between speakers, so a long carrier is split at voice pauses and carrier dips; a
    # carrier drop between overs is a turn change (2.5-4 s on the 2026-11-03 net), so pieces are not merged across
    # one. HF voice gate: no carrier, the VAD closes on a breath or between the letters of a call sign, so a short
    # piece waits `merge_gap_ms` for its continuation and a longer pre-roll gives back the syllable the VAD heard late.
    SEG_DEFAULTS = {
        "carrier": {"preroll_ms": 500, "hangover_ms": 900, "split_pause_ms": 800, "dip_split_ms": 96,
                    "merge_gap_ms": 0, "merge_max_s": 3.0, "min_split_s": 2.0, "pause_ratio": 0.35},
        "voice": {"preroll_ms": 600, "hangover_ms": 900, "split_pause_ms": 0, "dip_split_ms": 0,
                  "merge_gap_ms": 600, "merge_max_s": 3.0, "min_split_s": 2.0, "pause_ratio": 0.35},
    }

    def segmenter(self, r) -> Segmenter:
        d = self.SEG_DEFAULTS["carrier" if r["gate"] == "carrier" else "voice"]
        k = {key: r.get(key, v) for key, v in d.items()}
        return Segmenter(self._gate(r), SR, 32, r.get("min_speech_ms", 400), k.pop("hangover_ms"),
                         r.get("max_tx_s", 180), k.pop("preroll_ms"), r.get("partial_every_s", 1.0), **k)

    def _recent_tx(self, per_radio=1500) -> list[dict]:
        """Each radio's day (up to `per_radio`), so every net reaches the dashboard whole: its header counts, the
        accurate pass status and the Winlink time span all need the net's first transmission."""
        out, n = [], {}
        for t in reversed(self.tx):
            r = t.get("radio")
            if n.get(r, 0) < per_radio:
                n[r] = n.get(r, 0) + 1
                out.append(t)
        return out[::-1]

    def highpass_for(self, rid) -> int:
        r = self.radio_cfg.get(rid) or {}
        return int(r.get("highpass_hz", 300 if r.get("gate") == "carrier" else 0))

    ECHO = re.compile(r"^\W*(ham )?radio net( on [\d.]+ ?mhz)?\W*$", re.I)

    def _is_echo(self, rid, raw: str) -> bool:
        """Whisper's well-known failure on a clip it cannot read: it returns its own prompt."""
        words = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
        w = words(raw)
        return not w or bool(self.ECHO.match(raw)) or (len(w) > 8 and w in words(self.prompt_for(rid)))

    def apply_accurate(self, tid: str, res: dict):
        """Keep the home server's text beside the live text (never replacing it), log it, show it. A result that only
        repeats Whisper's prompt ("Radio net on 442.100 MHz.": 58 of 531 on 11-02) is kept as `accurate_echo` and the
        live text stays in use."""
        raw = (res.get("text") or "").strip()
        t0 = self._find(tid)
        stage = res.get("stage") or "final"
        cur = t0 and (t0.get("accurate") or t0.get("accurate_echo"))
        if stage == "provisional" and cur and cur.get("stage", "final") == "final":
            return                                               # a late provisional never undoes the final text
        extra = {k: res[k] for k in ("conf", "flags", "where") if res.get(k) not in (None, [])}   # confidence, where
        from .textclean import collapse_repeats, template_loop
        if (self._is_echo(t0.get("radio") if t0 else None, raw) or collapse_repeats(raw)[1] or template_loop(raw)
                or "low_conf" in (res.get("flags") or [])):
            # a prompt echo, a loop, or nothing heard (the context pass returns "" for a clip it set aside: a
            # courtesy beep read as "Thank you."): worse than the live text, so the live text stays in use
            echo = {"text": raw, "model": res.get("model"), "at": datetime.now().isoformat(timespec="seconds"),
                    "stage": stage, **extra}
            with self.lock:
                tx = self._find(tid)
                if not tx:
                    return
                tx.pop("accurate", None); tx["accurate_echo"] = echo
                tx.pop("consensus", None)                # made from other text: the next agreement redoes it
                self._rebuild_heard()
                tx = dict(tx)
            self._log({"update": tid, "accurate_echo": echo})
            self.bus.publish("tx", tx)
            return
        validate = self.callbook.validate if self.callbook else None
        text, calls = normalize(res.get("text", ""), roster=self.roster, validate=validate)
        text, calls, fix = callfix.apply(self, self._find(tid), text, calls, whole_net=True)   # near-miss calls
        acc = {"text": text, "callsigns": calls, "noise": probably_noise(text, calls),
               "model": res.get("model"), "at": datetime.now().isoformat(timespec="seconds"), "stage": stage,
               **extra, **fix}
        with self.lock:
            tx = self._find(tid)
            if not tx:
                return
            acc["speaker"], acc["speaker_how"] = speaker.from_text(text, calls, self._ncs_at(tx))
            tx["accurate"] = acc
            tx.pop("accurate_echo", None)
            tx.pop("consensus", None)                    # made from other text: the next agreement redoes it
            new = [c for c in calls if c not in self.heard]
            self._rebuild_heard()
            for c in new:
                if c in self.heard and not self.heard[c].get("card"):
                    self.heard[c]["card"] = self.callbook.card(c) if self.callbook else {"call": c, "sources": []}
            tx = dict(tx)
        self._log({"update": tid, "accurate": acc})
        self.bus.publish("tx", tx)
        self.bus.publish("heard", {"heard": self.heard_list()})
        if tx.get("status") == "final":
            self._hear_close(tx)                                 # a close the live text garbled

    def _ncs_at(self, tx: dict) -> str | None:
        """Net control of the scheduled net this transmission was heard in (at its own time: the accurate pass can
        arrive after the net has ended)."""
        try:
            at = datetime.fromisoformat(tx["start"])
        except (KeyError, TypeError, ValueError):
            at = self.clock()
        return (self._current_by_radio(at).get(tx.get("radio")) or {}).get("ncs")

    def voice_suggest(self, tid: str) -> dict:
        """Who?'s "sounds like K7ABC" (station/voicehint.py): the closest voice earlier in the same net."""
        with self.lock:
            tx = self._find(tid)
            if tx is None:
                raise LookupError(tid)
            items = list(self.tx)
        return self.voice.suggest(tx, items)

    def set_speaker(self, tid: str, call: str | None, suggested: str | None = None) -> dict:
        """The operator says who was talking ("Who?" on the dashboard); empty = back to automatic. Always wins.
        Kept like the accurate pass: an update line in the day's log, merged by load_today. `suggested` = what the
        voice suggestion offered (kept, so the operator's answers measure the suggestions)."""
        call = (call or "").strip().upper()
        if call and not re.fullmatch(r"[A-Z0-9][A-Z0-9/-]{2,11}", call):
            raise ValueError("not a callsign")
        with self.lock:
            tx = self._find(tid)
            if tx is None:
                raise LookupError(tid)
            tx["speaker_set"] = call or None
            tx = dict(tx)
        line = {"update": tid, "speaker_set": call or None}
        if suggested is not None:
            line["voice_suggested"] = (suggested or "").strip().upper() or None
        self._log(line)
        self.bus.publish("tx", tx)
        return tx

    # ---------- tap-to-correct (station/corrections.py learns from these) ----------
    MAX_CORRECTION = 4000

    def correct(self, tid: str, text: str | None = None, undo: bool = False) -> dict:
        """The operator's own text for a transmission ("✎ Fix"): it wins over the accurate pass and the live text
        everywhere (log, search, minutes, attendance, ICS-309/213, Winlink). Callsigns are found again in it. Kept
        like "Who?": an update line in the day's log ({"update": id, "corrected": {...}}, or None = Undo), merged by
        load_today and the history index, so it works for a transmission from any day."""
        live = None
        with self.lock:
            live = self._find(tid)
            raw = dict(live) if live is not None else None
        if raw is None:
            raw = self.history.tx_raw(tid) if re.fullmatch(r"\w+", str(tid)) else None
        if raw is None:
            raise LookupError(tid)
        if raw.get("status", "final") != "final":
            raise ValueError("the transmission is still being transcribed")
        if undo:
            cor = None
        else:
            text = " ".join(str(text or "").split())
            if not text:
                raise ValueError("the corrected text is empty (Undo correction goes back to the station's text)")
            if len(text) > self.MAX_CORRECTION:
                raise ValueError(f"at most {self.MAX_CORRECTION} characters")
            validate = self.callbook.validate if self.callbook else None
            shown = ((raw.get("corrected") or {}).get("from")
                     if raw.get("corrected") else ((raw.get("accurate") or {}).get("text") or raw.get("text") or ""))
            _, calls = normalize(text, roster=self.roster, validate=validate)
            # the operator's words stay as typed; the callsigns are read from them: spelled or known ones
            # (normalize), and any call the operator wrote in capitals (they are the authority here)
            typed = [c for c in re.findall(r"\b[A-Z0-9]{3,8}\b", text) if CALL_SHAPE.match(c)]
            calls = list(dict.fromkeys(calls + typed))
            calls.sort(key=lambda c: text.find(c) if c in text else len(text))   # in the order said
            cor = {"text": text, "callsigns": calls, "noise": False, "by": "operator",
                   "at": datetime.now().isoformat(timespec="seconds"), "from": shown}
            cor["speaker"], cor["speaker_how"] = speaker.from_text(text, calls, self._ncs_at(raw))
        with self.lock:
            live = self._find(tid)
            if live is not None:
                if cor is None:
                    live.pop("corrected", None)
                else:
                    live["corrected"] = cor
                self._rebuild_heard()
                for c in (cor or {}).get("callsigns", []):
                    if c in self.heard and not self.heard[c].get("card"):
                        self.heard[c]["card"] = self.callbook.card(c) if self.callbook else {"call": c, "sources": []}
                out = dict(live)
            else:
                out = dict(raw)
                out.pop("corrected", None)
                if cor:
                    out["corrected"] = cor
        self._log({"update": tid, "corrected": cor})
        self.history.flush()                      # minutes, attendance and search see it now; dashboards refresh
        self.bus.publish("tx", out)
        if live is not None:
            self.bus.publish("heard", {"heard": self.heard_list()})
        return out

    def learned(self, action: str | None = None, heard: str = "", say: str = "") -> dict:
        """Learned fixes: proposals from the corrections, and the accepted list (data/fixups_learned.yaml)."""
        from . import corrections
        store, rows = corrections.Learned(self.data), self.history.corrected_rows()
        if action:
            out = store.act(action, heard, say, rows)
            self.bus.publish("learned", {"accepted": len(out["accepted"]), "proposals": len(out["proposals"])})
            return out
        return store.view(rows)

    def accuracy(self) -> dict:
        """Word error rate of live / accurate / consensus text against the operator's corrections."""
        from . import corrections
        return corrections.accuracy(self.history.corrected_rows(), self.me, self.radios)

    def refix_ended_nets(self, now: datetime | None = None, quiet_min: float = 15):
        """When a net is over, run the near-miss callsign correction once more over its accurate text with the WHOLE
        net known (the follow-along pass saw only the net so far): a call heard late fixes an earlier garble."""
        from . import ics
        now = now or datetime.now()
        done = self.__dict__.setdefault("_refixed", set())
        with self.lock:
            tx = list(self.tx)
        for rid in self.radios:
            for s in ics.sessions(tx, rid):
                key = (rid, s["start"])
                if key in done or now - datetime.fromisoformat(s["end"]) < timedelta(minutes=quiet_min):
                    continue
                done.add(key)
                for t in ics.select(tx, rid, s["start"], s["end"], include_noise=True):
                    a = (self._find(t["id"]) or {}).get("accurate")
                    if not a or a.get("noise"):
                        continue
                    text, calls, fix = callfix.apply(self, self._find(t["id"]), a["text"], a.get("callsigns", []), whole_net=True)
                    if not fix.get("corrections"):
                        continue
                    acc = dict(a, text=text, callsigns=calls,
                               corrections=(a.get("corrections") or []) + fix["corrections"], maybe=fix.get("maybe", []))
                    with self.lock:
                        live = self._find(t["id"])
                        if not live:
                            continue
                        acc["speaker"], acc["speaker_how"] = speaker.from_text(text, calls, self._ncs_at(live))
                        live["accurate"] = acc
                        live.pop("consensus", None)
                        self._rebuild_heard()
                        pub = dict(live)
                    self._log({"update": t["id"], "accurate": acc})
                    self.bus.publish("tx", pub)

    # ---------- net-wide callsign agreement + local-name fix-ups (station/consensus.py, station/fixups.py) ----------
    def _licences(self):
        from .consensus import Licences
        cb = self.callbook
        if not (cb and getattr(cb, "con", None)):
            return None
        lic = self.__dict__.get("_licences_obj")
        if lic is None or lic.con is not cb.con:
            from hamkit.zipgeo import ZipGeo
            try:
                geo = ZipGeo()                                # the ZIP centres (python -m hamkit.zipgeo update)
            except Exception:  # noqa: BLE001 — distance is evidence, never a requirement
                geo = None
            lic = self._licences_obj = Licences(cb.con, getattr(cb, "lock", None), geo)
        return lic

    def where(self) -> tuple[float, float] | None:
        """The station's (lat, lon) for VHF/UHF distances (station/consensus.py where_of)."""
        from .consensus import where_of
        return where_of(self.cfg, (self._saved().get("operator") or {}).get("grid"), self._licences(), self.me)

    def _sched_entry(self, net_id: str | None) -> dict | None:
        return next((n for n in (self.cfg.get("schedule") or []) if net_id and n.get("id") == net_id), None)

    def agree_session(self, rid: str, start: str, end: str) -> dict:
        """Run the net-wide agreement over one net or conversation (start..end on `rid`) and keep, on each
        transmission it changes, a `consensus` record (an update line in the day log, like the accurate pass).
        Idempotent: a record equal to the one kept is not written again."""
        from . import consensus, fixups
        with self.lock:
            rows = sorted((t for t in self.tx if t.get("radio") == rid and t.get("status") == "final"
                           and start <= t["start"] <= end), key=lambda t: t["start"])
            rows = [json.loads(json.dumps(t)) for t in rows]           # a copy: the agreement runs unlocked
        if not rows:
            return {"radio": rid, "start": start, "end": end, "changed": 0}
        net_id = next(((t.get("net") or {}).get("id") for t in rows if (t.get("net") or {}).get("id")), None)
        try:
            key = self.history.instance_of(rid, start)
            prior = self.history.prior_checkins(net_id, start, exclude=key)
        except Exception:  # noqa: BLE001 — history is evidence, never a requirement
            prior = {}
        fx = fixups.load(self.root / "config", self.data)
        lic = self._licences()
        answers = {}
        if self.callbook and self.callbook.qrz:           # QRZ.com for the calls the FCC does not show Active
            heard = {c for t in rows for c in ((t.get("accurate") or {}).get("callsigns") or [])
                     + list(t.get("callsigns") or [])}
            try:
                answers = consensus.qrz_answers(heard, self.callbook.qrz, lic,
                                                allow_network=bool(self.cfg.get("qrz_online", True)))
            except Exception as e:  # noqa: BLE001 — QRZ is evidence, never a requirement
                print(f"consensus: QRZ: {type(e).__name__}: {e}", flush=True)
        recs, res = consensus.for_session(rows, lic, prior, callfix.home_state(self), self.roster,
                                          self.me, fx, fixups.net_names_for(self._sched_entry(net_id)),
                                          where=self.where(), qrz=answers, reach_km=self.reach_km())
        changed = []
        unconf = self.__dict__.setdefault("unconfirmed", {})
        with self.lock:
            for e in res["calls"].values():
                if e.get("status") != "distant":
                    unconf[e["call"]] = e.get("unconfirmed")      # None = confirmed
            for t in rows:
                live = self._find(t["id"])
                if not live:
                    continue
                rec = recs.get(t["id"])
                if live.get("corrected"):
                    continue          # the operator's text wins; the record made before it stays (accuracy uses it)
                if (live.get("accurate") or live).get("text") != (t.get("accurate") or t).get("text"):
                    continue                                          # new text arrived meanwhile: next time
                if live.get("consensus") == rec:
                    continue
                if rec:
                    live["consensus"] = rec
                else:
                    live.pop("consensus", None)
                changed.append(dict(live))
            if changed or any((self.heard.get(c) or {}).get("unconfirmed") != w for c, w in unconf.items()
                              if c in self.heard):
                self._rebuild_heard()
                for c, e in self.heard.items():
                    if e.get("card") is None:
                        e["card"] = self.callbook.card(c) if self.callbook else {"call": c, "sources": []}
        for t in changed:
            self._log({"update": t["id"], "consensus": t.get("consensus")})
            self.bus.publish("tx", t)
        self.bus.publish("heard", {"heard": self.heard_list()})
        return {"radio": rid, "start": start, "end": end, "changed": len(changed),
                "moved": {h: e for h, e in res["calls"].items() if e["call"] != h},
                "unverified": sorted(h for h, e in res["calls"].items() if e["status"] in ("unverified", "lapsed")),
                "distant": sorted(h for h, e in res["calls"].items() if e["status"] == "distant")}

    def agree_recent(self, now: datetime | None = None, hours: float = 12) -> list[dict]:
        """The agreement for every net heard in the last `hours` whose transcript changed since its last run: while a
        net runs (the net so far, cheap: FCC answers are cached) and once more after it, with the final text."""
        from . import ics
        if not self.cfg.get("consensus", True):
            return []
        now = now or self.clock()
        seen = self.__dict__.setdefault("_agreed", {})
        out = []
        with self.lock:
            tx = list(self.tx)
        for rid in self.radios:
            for s in ics.sessions(tx, rid):
                if now - datetime.fromisoformat(s["end"]) > timedelta(hours=hours):
                    continue
                fp = hash(tuple((t["id"], (t.get("accurate") or t).get("text"), bool(t.get("consensus")))
                                for t in tx if t.get("radio") == rid and t.get("status") == "final"
                                and s["start"] <= t["start"] <= s["end"]))
                if seen.get((rid, s["start"])) == fp:
                    continue
                out.append(self.agree_session(rid, s["start"], s["end"]))
                with self.lock:
                    seen[(rid, s["start"])] = hash(tuple(
                        (t["id"], (t.get("accurate") or t).get("text"), bool(t.get("consensus")))
                        for t in self.tx if t.get("radio") == rid and t.get("status") == "final"
                        and s["start"] <= t["start"] <= s["end"]))
        return out

    def agree_recent_async(self):
        th = self.__dict__.get("_agree_thread")
        if th and th.is_alive():
            return
        def run():
            try:
                self.agree_recent()
            except Exception as e:  # noqa: BLE001 — the agreement is extra; never stop the station
                print(f"consensus: {type(e).__name__}: {e}", flush=True)
        self._agree_thread = threading.Thread(target=run, daemon=True, name="consensus")
        self._agree_thread.start()

    def accurate_now(self, rid: str, start: str | None = None, end: str | None = None, force=False) -> dict:
        from . import ics
        if rid not in self.radios:
            raise KeyError(rid)
        if not (start and end):
            with self.lock:
                b = ics.session_bounds(list(self.tx), rid)
            if not b:
                raise ValueError("no transcribed transmissions on this radio yet")
            start, end = b
        return self.accurate.submit(rid, start, end, force=force)

    # ---------- the over-the-air test (station/wer.py) ----------
    def _tests_path(self) -> Path:
        return self.data / "tests.jsonl"

    def test_history(self, n=20) -> list[dict]:
        try:
            rows = [json.loads(x) for x in self._tests_path().read_text(encoding="utf-8").splitlines() if x.strip()]
        except OSError:
            return []
        latest = {}
        for r in rows:
            latest[r["tx"]] = r                                  # a re-score (after the accurate pass) replaces
        return sorted(latest.values(), key=lambda r: r["start"], reverse=True)[:n]

    def score_test(self, rid: str, tid: str | None = None) -> dict:
        """Score the radio's last transmission (or `tid`) against the test text; queue the accurate pass for it."""
        from . import wer
        with self.lock:
            fin = [t for t in self.tx if t.get("radio") == rid and t.get("status") == "final"
                   and (t["id"] == tid if tid else True)]
            if not fin:
                raise ValueError("no transmission to score yet: read the test text over the air first")
            t = dict(fin[-1])
        me = self.cfg.get("callsign")
        row = {"tx": t["id"], "radio": rid, "start": t["start"], "freq_mhz": t.get("freq_mhz"),
               "duration_s": t.get("duration_s"), "live": {**wer.wer(wer.test_text(me), t.get("text", ""), me),
                                                           "text": t.get("text", ""), "model": t.get("model")}}
        if t.get("accurate"):
            a = t["accurate"]
            row["accurate"] = {**wer.wer(wer.test_text(me), a.get("text", ""), me), "text": a.get("text", ""), "model": a.get("model")}
        elif getattr(self, "accurate", None) and t.get("wav"):
            self.accurate.submit(rid, t["start"], t["start"])
            row["accurate_pending"] = True
        row["scored_at"] = datetime.now().isoformat(timespec="seconds")
        with open(self._tests_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def clean(self, rid, audio: np.ndarray) -> np.ndarray:
        """What Whisper hears: the radio's audio with everything under `highpass_hz` removed. An FM data jack
        passes the repeater's sub-audible tone (67-254 Hz) and any mains hum (60/120/180/240 Hz) along with the
        voice; on the 145.900 net they were the strongest sounds in the recording."""
        hz = self.highpass_for(rid)
        if not hz or len(audio) < 64:
            return audio
        # FFT high-pass with a 100 Hz cosine ramp (numpy only; segments are seconds to a few minutes long)
        n = len(audio)
        f = np.fft.rfftfreq(n, 1 / SR)
        gain = np.clip((f - (hz - 100)) / 100, 0, 1)
        gain = 0.5 - 0.5 * np.cos(np.pi * gain)
        return np.fft.irfft(np.fft.rfft(audio.astype(np.float64)) * gain, n).astype(np.float32)

    def live_busy(self) -> bool:
        """The live text needs the machine: a transmission is being received, or a live final is queued or being
        transcribed. The accurate pass on this PC holds while this is true (station/accurate_local.py)."""
        with self.lock:
            if any(r.get("live_tx") for r in self.radios.values()):
                return True
        return self._final_busy or not self.final_q.empty()

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
            from .radios import pick_index
            idx = pick_index(name, list(sd.query_devices()))    # exact name first: "Digirig Lite" ≠ "… NET-REC"
            if idx is None and self._audio_rescan(name):
                idx = pick_index(name, list(sd.query_devices()))
            if idx is None:
                from .radios import why_missing
                raise RuntimeError(why_missing(name) or f"audio input containing '{name}' not found")
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
            if self.radios[rid].get("paused"):
                if self.radios[rid].get("state") != "paused":
                    self._set_radio(rid, state="paused", carrier=False, level_db=None, snr_db=None)
                self.stop.wait(0.5)
                continue
            try:
                seg, meter = self.segmenter(r), self.make_meter(r)
                raw = RawRing(self.data / "raw" / rid, int(r.get("raw_keep_min", self.cfg.get("raw_keep_min", 60))))
                with self._source(r) as frames:
                    self._set_radio(rid, state="listening", error=None)
                    last = 0.0
                    while not self.stop.is_set():
                        if self.radios[rid].get("paused"):          # operator paused: finish what we have, close the device
                            self._events(rid, seg, seg.flush())
                            break
                        frame = frames.get(timeout=5)
                        raw.add(frame)
                        self._morse_feed(rid, frame)
                        tap = self.level_tap.get(rid)
                        if tap is not None:
                            tap.append(frame)
                        self.handle(rid, seg, frame)
                        m = meter.feed(frame, seg.active, seg.gate)
                        if m:
                            m["id"] = rid
                            self.bus.publish("m", m)
                            self.level_recent[rid].append((time.time(), m["rms"], m["peak"], m["rx"]))
                            self.silence[rid].feed(time.time(), m["rms"], m["rx"])
                        if time.time() - last > 0.25:
                            last = time.time()
                            self._set_radio(rid, level_db=round(seg.level_db, 1), carrier=seg.active,
                                            snr_db=m["snr"] if m else self.radios[rid].get("snr_db"))
            except Exception as e:  # noqa: BLE001 — keep trying; a radio unplugged is not a crash
                self._set_radio(rid, state="error", error=f"{type(e).__name__}: {e}", carrier=False)
                self.stop.wait(10)

    _rescan_lock = threading.Lock()
    _rescan_at = 0.0

    def _audio_rescan(self, name: str) -> bool:
        """A radio's input is not in this process's device list: if Windows has it now (renamed, re-plugged, a new
        driver), re-read the audio devices here (at most once a minute). PortAudio closes every open stream when it
        re-reads, so the other radios reconnect within ~10 s (their loops reopen their inputs)."""
        from . import radios as rmod
        with self._rescan_lock:
            if time.time() - Station._rescan_at < 60:
                return False
            now = rmod.fresh_inputs()
            if not now or rmod.base_name(name) not in now:
                Station._rescan_at = time.time() - 30           # look again in ~30 s, not every 10
                return False
            import sounddevice as sd
            sd._terminate(); sd._initialize()
            Station._rescan_at = time.time()
            print(f"{datetime.now():%H:%M:%S} audio: '{name}' is back in Windows; sound devices re-read", flush=True)
            return True

    def _morse_feed(self, rid, frame):
        w = self.morse.get(rid)
        if w is None:
            return
        try:
            x = np.asarray(frame, np.float32)
            x = x.mean(axis=1) if x.ndim > 1 else x
            for run in w.feed(x, time.time() - len(x) / SR):
                self.morse_found(rid, run)
        except Exception as e:  # noqa: BLE001 — Morse never stops listening
            print(f"morse: {type(e).__name__}: {e}", flush=True)

    def morse_found(self, rid, run: dict):
        """A decoded CW run: attached to the transmission it was heard in, else a transcript row of its own."""
        info = {k: run[k] for k in ("tone_hz", "wpm", "symbols", "text", "chars", "score", "t0", "t1")}
        print(f"{datetime.now():%H:%M:%S} {rid} MORSE {info['wpm']:.0f} wpm {info['tone_hz']} Hz: {info['text']}",
              flush=True)
        with self.lock:
            hit = None
            for t in reversed(self.tx[-30:]):
                if t.get("radio") != rid or t.get("kind") == "cw":
                    continue
                a = datetime.fromisoformat(t["start"]).timestamp()
                b = a + (t.get("duration_s") or (time.time() - a))
                if a - 1.0 <= info["t1"] and info["t0"] <= b + 1.0:
                    hit = t
                    break
            if hit is not None:
                hit.setdefault("morse", []).append(info)
                tx = hit
            else:
                self.next_id += 1
                tid = str(self.next_id)
                r = self.radios[rid]
                start = datetime.fromtimestamp(info["t0"])
                tx = {"id": tid, "radio": rid, "radio_name": r["name"], "freq_mhz": r["freq_mhz"],
                      "start": start.isoformat(timespec="seconds"), "status": "final", "kind": "cw",
                      "text": info["text"], "partial": "", "callsigns": [], "speaker": None, "terms": [],
                      "duration_s": round(info["t1"] - info["t0"], 2), "morse": [info]}
                try:
                    calls = normalize(info["text"], roster=self.roster,
                                      validate=self.callbook.validate if self.callbook else None)[1]
                    tx["callsigns"] = calls
                except Exception:  # noqa: BLE001
                    pass
                try:
                    net = self.net_of(rid, start, r.get("freq_mhz"))
                    if net:
                        tx["net"] = net
                except Exception:  # noqa: BLE001
                    pass
                if run.get("audio") is not None and len(run["audio"]):
                    wav = self.data / "recordings" / start.strftime("%Y%m%d") / f"{tid}_{rid}_cw.wav"
                    wav.parent.mkdir(parents=True, exist_ok=True)
                    sf.write(wav, run["audio"], SR, subtype="PCM_16")
                    tx["wav"] = str(wav.relative_to(self.data))
                self.tx.append(tx)
                del self.tx[:-self.KEEP_TX]
        self._log(tx if hit is None else {"update": tx["id"], "morse": tx["morse"]})
        self.bus.publish("tx", tx)

    def handle(self, rid, seg: Segmenter, frame: np.ndarray):
        self._events(rid, seg, seg.feed(frame))

    def _events(self, rid, seg: Segmenter, events):
        for ev in events:
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
                print(f"{datetime.now():%H:%M:%S} {rid} {'split at a pause' if i.get('split') else 'gate CLOSE'}"
                      f" {i['duration_s']}s" + (" (a short piece kept with its neighbour)" if i.get("merged") else "")
                      + (" -> dropped (shorter than min_speech_ms)" if i["kerchunk"] else ""), flush=True)
                self._tx_end(rid, ev[1], ev[2])

    def _live_id(self, rid):
        return self.radios[rid].get("live_tx")

    def _tx_start(self, rid, t0):
        if self.awake_mode == "traffic":                # a dark laptop runs ~5x slower: hold from the first moment
            threading.Thread(target=self._keep_awake_tick, daemon=True).start()
        with self.lock:
            self.next_id += 1
            tid = str(self.next_id)
            self.radios[rid]["live_tx"] = tid
            r = self.radios[rid]
            start = datetime.fromtimestamp(t0)
            tx = {"id": tid, "radio": rid, "radio_name": r["name"], "freq_mhz": r["freq_mhz"],
                  "start": start.isoformat(timespec="seconds"), "status": "live",
                  "partial": "", "text": "", "callsigns": [], "speaker": None, "terms": []}
            try:
                net = self.net_of(rid, start, r.get("freq_mhz"))   # which scheduled net (and occurrence) it belongs to
            except Exception:  # noqa: BLE001
                net = None
            after = self.closer.after_close(rid, start, net)   # the net was closed: a new conversation starts
            if after:
                tx["after"] = after
                if net and net.get("key") == after:
                    net = None
            if net:
                tx["net"] = net
            self.tx.append(tx)
            del self.tx[:-self.KEEP_TX]
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

    def _check_own_call(self, eng, tx, audio, res: dict) -> dict:
        """The live prompt spells the operator's call (it fixes their own transmissions), and Whisper sometimes writes
        it into someone else's: on one station 15 live texts had the call and the accurate pass agreed on 2
        ("W9XYZ, Whiskey Nine…" over "Roger, <town>"). When the text has the operator's call, decode once more
        without it in the prompt; keep the call only if that decode shows it too (the same call within one
        character, or "Whiskey 9 / Niner" spoken), else use that decode. Measured on those 15: 3 kept, all right;
        12 dropped, 11 right (one spoken call the second decode also missed). ~1 % of transmissions pay one more decode."""
        me = (self.me or "").split("-")[0]
        if not me or not own_call_in(res.get("text", ""), me):
            return res
        try:
            alt = eng.transcribe(audio, prompt=self.prompt_for(tx and tx["radio"], own_call=False))
        except Exception:  # noqa: BLE001 — keep what we have
            return res
        if own_call_shown(alt.get("text", ""), me):
            return res
        print(f"{datetime.now():%H:%M:%S} own call not confirmed without the prompt: {res.get('text', '')[:60]!r} -> "
              f"{alt.get('text', '')[:60]!r}", flush=True)
        return {**alt, "own_call_dropped": res.get("text", "")}

    def prompt_for(self, rid: str | None, own_call=True) -> str:
        """Short on purpose (see engine.make): frequency + the operator's call, spelled phonetically.
        Measured on the test PC: fixes 'This is a 79' -> 'This is W9XYZ' with no loops. The accurate pass gets no
        call (own_call=False): large-v3 with it wrote W9XYZ into 12 of 161 transmissions of a net W9XYZ was not in."""
        r = self.radios.get(rid) or {}
        parts = [f"Ham radio net on {r['freq_mhz']:.3f} MHz." if r.get("freq_mhz") else "Ham radio net."]
        if self.me and own_call:
            parts.append(f"This is {self.me}, {phonetic(self.me)}. Over.")
        return " ".join(parts)

    # ---------- workers ----------
    @staticmethod
    def _warm(eng):
        """The first GPU run compiles kernels (+1-2 s on the test PC); pay that at startup, not on air."""
        try:
            if getattr(eng, "warm", True):
                eng.transcribe(np.zeros(SR * 2, np.float32))
        except Exception:  # noqa: BLE001
            pass

    # ---------- dashboards open + live engine check (owner 2026-11-07) ----------
    VIEWER_GRACE_S = 120        # a reload or a dropped connection is not "closed"
    SUSPECT_S = 3.0             # a transmission this long that reads as one word or nothing is suspect
    RELOAD_EVERY_S = 300        # at most one engine reload per 5 minutes

    def viewer(self, opened: bool):
        """server.py: a browser opened (True) or closed (False) the dashboard's event stream."""
        with self.lock:
            first = opened and not self.dashboard_open()
            self.viewers = max(0, self.viewers + (1 if opened else -1))
            self.viewer_seen = time.time()
        if first:
            self._engine_check = True
            if getattr(self, "awake", None) is not None:
                threading.Thread(target=self._keep_awake_tick, daemon=True).start()   # hold now, not at the next tick

    def dashboard_open(self) -> bool:
        return self.viewers > 0 or (self.viewer_seen is not None and time.time() - self.viewer_seen < self.VIEWER_GRACE_S)

    @staticmethod
    def _words(text: str) -> int:
        return len(re.findall(r"[A-Za-z0-9]+", text or ""))

    def _engine_broken(self, eng) -> bool:
        """Re-read the reference transmission: a live engine that read it well and now cannot is broken."""
        if self._canary is None:
            return False
        audio, words, prompt = self._canary            # read it as it was read: without its prompt a noisy reference
        try:                                           # fell to one word on a good engine (8 false reloads on one station)
            return self._words(eng.transcribe(audio, prompt=prompt).get("text")) <= 1 < words
        except Exception:  # noqa: BLE001
            return True

    def _reload_final(self, eng, why: str):
        """A fresh live engine (and the partial one at its next run). Measured 2026-11-07: after Modern Standby the
        long-lived GPU pipeline wrote "The." for clear speech while a fresh one read it right."""
        self._engine_reload_at = time.time()
        print(f"{datetime.now():%H:%M:%S} live engine: {why}; reloading it", flush=True)
        try:
            new = self._final_factory()
            self._warm(new)
        except Exception as e:  # noqa: BLE001
            print(f"live engine: reload failed: {type(e).__name__}: {e}", flush=True)
            return eng
        if self._engine_broken(new):                   # the fresh engine cannot read it either: the reference is bad
            print("live engine: a fresh engine fails the reference too; the reference is dropped, the engine was fine",
                  flush=True)
            self._canary = None
        del eng
        self._partial_reload = True
        self.engines.update(final=new.name, status="ready", reloaded=datetime.now().isoformat(timespec="seconds"))
        self.bus.publish("engines", self.engines)
        return new

    def _maybe_recover(self, eng, audio, res: dict, prompt: str):
        """-> (engine, result). Only while a dashboard is open (owner): a suspect final -> check the engine on the
        reference; broken -> reload and read the transmission again. A good final becomes the new reference."""
        n = self._words(res.get("text"))
        if n >= 4 and len(audio) <= 15 * SR:
            self._canary = (audio, n, prompt)
            return eng, res
        if (n > 1 or len(audio) < self.SUSPECT_S * SR or not self.dashboard_open()
                or time.time() - self._engine_reload_at < self.RELOAD_EVERY_S or not self._engine_broken(eng)):
            return eng, res
        eng = self._reload_final(eng, f"read {len(audio) / SR:.1f} s as {res.get('text')!r} and failed the reference")
        try:
            again = eng.transcribe(audio, prompt=prompt)
            print(f"live engine: read again: {again.get('text', '')[:80]!r}", flush=True)
            return eng, again
        except Exception:  # noqa: BLE001
            return eng, res

    def _partial_worker(self):
        try:
            eng = self._partial_factory()
            self._warm(eng)
        except Exception as e:  # noqa: BLE001
            self.engines["partial_error"] = str(e); return
        self.engines["partial"] = eng.name
        while not self.stop.is_set():
            if self._partial_reload:
                self._partial_reload = False
                try:
                    new = self._partial_factory(); self._warm(new); del eng; eng = new
                except Exception as e:  # noqa: BLE001
                    print(f"partial engine: reload failed: {type(e).__name__}: {e}", flush=True)
            if not self.partial_ev.wait(0.5):
                continue
            self.partial_ev.clear()
            for rid, (tid, audio) in list(self.partial_slot.items()):
                if self.partial_slot.get(rid, (None,))[0] != tid:
                    continue
                text = eng.transcribe(self.clean(rid, audio[-30 * SR:]), prompt=self.prompt_for(rid))["text"]
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
                if self._engine_check:                  # a dashboard just opened: is the live engine still good?
                    self._engine_check = False
                    if time.time() - self._engine_reload_at >= self.RELOAD_EVERY_S and self._engine_broken(eng):
                        eng = self._reload_final(eng, "failed the reference when the dashboard opened")
                continue
            self._final_busy = True
            try:
                t = time.time()
                tx = self._find(tid)
                clean = self.clean(tx and tx["radio"], audio)
                prompt = self.prompt_for(tx and tx["radio"])
                res = eng.transcribe(clean, prompt=prompt)
                eng, res = self._maybe_recover(eng, clean, res, prompt)
                res = self._check_own_call(eng, tx, clean, res)
                self.finish(tid, res, round(time.time() - t, 2))
            finally:
                self._final_busy = False

    # ---------- enrichment ----------
    def finish(self, tid: str, res: dict, took_s: float = 0.0):
        validate = self.callbook.validate if self.callbook else None
        text, calls = normalize(res["text"], roster=self.roster, validate=validate)
        text, calls, fix = callfix.apply(self, self._find(tid), text, calls)                    # near-miss calls
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
            who, how = speaker.from_text(text, calls, net.get("ncs"))
            tx.update(status="final", text=text, callsigns=calls, speaker=who, speaker_how=how, terms=terms,
                      noise=probably_noise(text, calls),
                      model=res.get("model"), looped=res.get("looped", False), took_s=took_s,
                      hint_keys=[c["key"] for c in h["cards"]], partial="", **fix)
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
        self.voice.queue(tx)
        if getattr(self, "accurate", None):
            self.accurate.on_final(tx)                           # the accurate pass follows the live text
        self._hear_close(tx)
        if new and self.callbook and self.callbook.qrz and self.cfg.get("qrz_online", True):
            threading.Thread(target=self._qrz_refresh, args=(new,), daemon=True).start()

    def _hear_close(self, tx):
        try:                                                     # did net control just close the net?
            with self.lock:
                t = dict(self._find(tx["id"]) or tx)
            self.closer.on_tx(t)
        except Exception as e:  # noqa: BLE001 — never let close detection stop the station
            print(f"netclose: {type(e).__name__}: {e}", flush=True)

    def _qrz_refresh(self, calls):
        for c in calls:
            card = self.callbook.card(c, qrz_network=True)
            with self.lock:
                if c in self.heard:
                    self.heard[c]["card"] = card
            self.bus.publish("heard", {"heard": self.heard_list()})

    def _urgent(self):
        try:
            return self.history.urgent_open()
        except Exception:  # noqa: BLE001
            return []

    def heard_list(self):
        with self.lock:
            return sorted(self.heard.values(), key=lambda e: e.get("last", ""), reverse=True)

    # ---------- persistence ----------
    def _log_path(self, day: datetime | None = None) -> Path:
        return self.data / "logs" / f"{(day or self.clock()).strftime('%Y%m%d')}.jsonl"

    def _log(self, tx):
        p = self._log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(tx)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:                                                  # the history index follows the log
            self.history.ingest(json.loads(line))
            self.history.mark_logged(p)
        except Exception as e:  # noqa: BLE001 — the log is the record; the index catches up at the next start
            print(f"history: {type(e).__name__}: {e}", flush=True)

    def load_today(self):
        """Yesterday's log, then today's: after a restart the dashboard still shows last night's nets under
        Completed (an update line for one of yesterday's transmissions may be in today's file, so the order matters)."""
        today = self._log_path()
        yday = self._log_path(self.clock() - timedelta(days=1))
        for p in (yday, today):
            if p.exists():
                self._replay(p, live=p == today)
        self._rebuild_heard()                                 # counts by the accurate callsigns where there are some
        for c, e in self.heard.items():
            e["card"] = self.callbook.card(c) if self.callbook else {"call": c, "sources": []}
        for rid, h in self.hinters.items():
            self.hints.setdefault(rid, {"cards": [], "reminders": [], "net": {}})["net"] = h.classify()
        del self.tx[:-self.KEEP_TX]
        self.next_id = max([self.next_id] + [int(t["id"]) for t in self.tx if str(t["id"]).isdigit()])

    def _replay(self, p: Path, live: bool):
        """One day's log into memory. `live`: today's, which also primes the net-type hints."""
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                tx = json.loads(line)
            except ValueError:
                continue
            if "update" in tx:                                # added after the live text: accurate pass, "Who?", ✎ Fix
                for t in self.tx:
                    if t.get("id") == tx["update"]:
                        t.update({k: v for k, v in tx.items() if k in ("accurate", "accurate_echo", "speaker_set",
                                                                      "net", "after", "corrected", "consensus",
                                                                      "morse", "voice_suggested")})
                        for k in ("net", "after", "corrected", "consensus"):   # a net close / a correction undone
                            if k in tx and tx[k] is None:
                                t.pop(k, None)
                        if "accurate" in tx:
                            t.pop("accurate_echo", None)
                        if "accurate_echo" in tx:
                            t.pop("accurate", None)
                        if ("accurate" in tx or "accurate_echo" in tx) and "consensus" not in tx:
                            t.pop("consensus", None)            # new text: the agreement is made again
                continue
            cut = (self.radios.get(tx.get("radio")) or {}).get("cleared_at")
            if cut and tx.get("start", "") < cut:
                continue
            self.tx.append(tx)
            for c in tx.get("callsigns", []):
                e = self.heard.setdefault(c, {"call": c, "count": 0, "first": tx["start"], "card": None})
                e["count"] += 1; e["last"] = tx["start"]
            if live:
                self.hinters.setdefault(tx.get("radio"), Hinter(self.me)).on_transmission(
                    tx.get("text", ""), tx.get("callsigns", []), at=datetime.fromisoformat(tx["start"]))

    def snapshot(self) -> dict:
        with self.lock:
            return {"callsign": self.me, "radios": list(self.radios.values()), "tx": self._recent_tx(),
                    "heard": self.heard_list(), "hints": self.hints, "net": self.schedule_now(),
                    "schedule": self.nets.decorated(), "engines": self.engines,
                    "alerts": self.nets.alerts.last, "netclose": self.closer.state(), "urgent": self._urgent(), "history": self.history.status,
                    "accurate": self.accurate.status() if getattr(self, "accurate", None) else [],
                    "accurate_available": self.accurate.available() if getattr(self, "accurate", None) else False,
                    "accurate_where": self.accurate.where if getattr(self, "accurate", None) and self.accurate.enabled else None,
                    "accurate_rate": round(self.accurate.rate, 2) if getattr(self, "accurate", None) else None}
