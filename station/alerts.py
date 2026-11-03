"""The pre-net alert: `alert_min` minutes (default 5) before each occurrence of a scheduled net, every open
dashboard shows "Tuesday Net starts at 19:00 on FTM-150R: set it to 146.940 MHz FM" with three answers:

    ready    "Radio is set": logs the radio at the net's frequency and mode (like Change) and says it is ready
    snooze   "Remind me in 1 min"
    skip     "Skip this net": no more alert, and "listen only during scheduled nets" leaves the radio paused
    dismiss  closes a "ready" / "already on frequency" alert

The alert is the station's, not one browser's: an answer on any screen (PC or phone) clears it on all of them
(event "alerts"). It stays up from `alert_min` before the start to STAY_MIN after it, so net control sees the
"still open" read-out as the net opens. Answers are kept in data/alerts.json (a restart keeps a skip).
When the radio's logged frequency already matches, it says so ("already") instead of asking.
"""
import json
import os
import threading
from datetime import datetime, timedelta

from . import recur

STAY_MIN = 10          # the alert (and the read-out for net control) stays this long after the net starts
SNOOZE_MIN = 1


class NoSuchAlert(LookupError):
    pass


def text_of(a: dict) -> str:
    """What a system notification says."""
    when = f"starts at {a['start'][11:16]}" if a["phase"] == "soon" else f"started at {a['start'][11:16]}"
    s = f"{a['name']} {when} on {a['radio_name']}"
    if a["state"] == "already" or a["state"] == "ready":
        return s + (f": radio on {a['freq_mhz']:.3f} MHz, ready" if a.get("freq_mhz") else ": ready")
    if a.get("freq_mhz"):
        return s + f": set it to {a['freq_mhz']:.3f} MHz" + (f" {a['mode']}" if a.get("mode") else "")
    return s


class Alerts:
    def __init__(self, station, schedule):
        self.st, self.sched = station, schedule
        self.lock = threading.RLock()
        self.state = self._read()
        self.last: list[dict] = []
        self._sig = None
        self._open_cache: dict[str, tuple[datetime, dict]] = {}

    # ---------- storage ----------
    @property
    def path(self):
        return self.st.data / "alerts.json"

    def _read(self) -> dict:
        try:
            s = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        return {k: dict(s.get(k) or {}) for k in ("ready", "skip", "dismiss", "snooze")}

    def _write(self):
        cut = (self.st.clock() - timedelta(days=3)).isoformat()
        for k in self.state:                            # old answers are forgotten (keys hold the date)
            self.state[k] = {x: v for x, v in self.state[k].items() if v >= cut}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def skipped(self) -> set[str]:
        return set(self.state["skip"])

    # ---------- what is due ----------
    def _open_counts(self, net_id: str, at: datetime) -> dict | None:
        h = getattr(self.st, "history", None)
        if not h:
            return None
        hit = self._open_cache.get(net_id)
        if hit and at - hit[0] < timedelta(seconds=60):
            return hit[1]
        try:
            c = h.open_counts(net_id, at)
        except Exception:  # noqa: BLE001 — the alert never depends on the history
            c = None
        self._open_cache[net_id] = (at, c)
        return c

    def active(self, at: datetime | None = None) -> list[dict]:
        at = at or self.st.clock()
        out = []
        for n in self.st.cfg.get("schedule") or []:
            lead = int(n.get("alert_min", 5))
            if lead <= 0:
                continue
            for o in recur.occurrences(n, at - timedelta(minutes=STAY_MIN + 1), at + timedelta(minutes=lead + 1)):
                s = o["start"]
                if not (s - timedelta(minutes=lead) <= at < s + timedelta(minutes=STAY_MIN)):
                    continue
                key = recur.key(n["id"], o["date"])
                if key in self.state["skip"] or key in self.state["dismiss"]:
                    continue
                sn = self.state["snooze"].get(key)
                if sn and datetime.fromisoformat(sn) > at:
                    continue
                r = self.st.radios.get(n.get("radio")) or {}
                f, mode = o.get("freq_mhz"), o.get("mode")
                on = bool(f and r.get("freq_mhz") and abs(f - r["freq_mhz"]) < 0.0005
                          and (not mode or mode == r.get("mode")))
                from .schedule import item
                a = item(n, o) | {"radio_name": r.get("name") or n.get("radio"), "alert_min": lead,
                                  "radio_freq_mhz": r.get("freq_mhz"), "radio_mode": r.get("mode"),
                                  "phase": "soon" if at < s else "started",
                                  "minutes": max(0, int((s - at).total_seconds() + 59) // 60),
                                  "state": "ready" if key in self.state["ready"] else "already" if on else "due",
                                  "open": self._open_counts(n["id"], at)}
                a["text"] = text_of(a)
                out.append(a)
        return sorted(out, key=lambda a: a["start"])

    def tick(self, at: datetime | None = None):
        """Every second: publish the alerts when what they say changes (a new one, a minute less, an answer)."""
        with self.lock:
            cur = self.active(at)
            sig = [(a["key"], a["state"], a["phase"], a["minutes"]) for a in cur]
            if sig != self._sig:
                self._sig, self.last = sig, cur
                self.st.bus.publish("alerts", {"alerts": cur})

    def act(self, key: str, action: str) -> dict:
        """An answer from a dashboard: ready | snooze | skip | dismiss."""
        if action not in ("ready", "snooze", "skip", "dismiss"):
            raise ValueError("unknown answer")
        at = self.st.clock()
        with self.lock:
            a = next((x for x in self.active(at) if x["key"] == key), None)
            if a is None and action != "skip":
                raise NoSuchAlert(key)
            if action == "ready" and a and a.get("freq_mhz") and a["state"] == "due":
                self.st.set_radio(a["radio"], a["freq_mhz"], a.get("mode"))     # logs it, like Change
            if action == "snooze":
                self.state["snooze"][key] = (at + timedelta(minutes=SNOOZE_MIN)).isoformat(timespec="seconds")
            else:
                self.state[action][key] = at.isoformat(timespec="seconds")
            self._write()
            self._sig = None
            print(f"{datetime.now():%H:%M:%S} alert {key}: {action}", flush=True)
        self.sched.tick(at)                       # a skip can pause a radio at once; every screen updates
        return {"alerts": self.last}
