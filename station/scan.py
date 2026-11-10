"""HF scan: on a radio the station tunes (CAT), look for conversations between nets, and remember where they were.

Owner 2026-11-10: "add a scan function to detect conversations/nets that currently have traffic. Consider best bands
for time of day and for my location and current antenna setup ... if a net is quiet do a scan for other
conversations ... turn on the auto scan, turn it off or put it on automatic always respecting a net first", and
"could it remember HF frequencies with good traffic?"

Per radio (its card): Scan  Off | On | Auto
  On    scan in the gaps between nets (a net always wins: scanning stops PRE_NET_MIN before one starts).
  Auto  the same, and also while a scheduled net is quiet (nothing heard for QUIET_MIN and no other net on).
How one cycle works (receive only; the only CAT commands are set frequency/mode and read S-meter):
  1. Pick a band: the bands open at this hour (BY_HOUR, local time), that the antenna works on (the card's antenna
     description), learned spots first.
  2. Remembered spots for this hour on that band are tried first, then a sweep of the band's phone segment in
     STEP_KHZ steps reading the S-meter; anything SIGNAL_OVER counts above the band's noise floor is a candidate.
  3. Park on each candidate (strongest first) for DWELL_S with the radio listening; speech transcribed there (not
     noise) = a conversation: stay while it goes on (quiet for STAY_QUIET_S ends it), and remember the spot.
The operator's own tuning wins: a frequency the scan did not set ends scanning for HOLD_MIN. Spots are kept in
data/scan_spots.json (never committed): frequency, mode, band, hours and weekdays heard, count, last heard, calls.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from datetime import datetime, timedelta

BANDS = {  # US phone segments (receive: anyone's conversations), sideband by convention
    "160": (1.843, 2.000, "LSB"), "80": (3.600, 4.000, "LSB"), "40": (7.125, 7.300, "LSB"),
    "20": (14.150, 14.350, "USB"), "17": (18.110, 18.168, "USB"), "15": (21.200, 21.450, "USB"),
    "12": (24.930, 24.990, "USB"), "10": (28.300, 28.850, "USB"),
}
BY_HOUR = [  # local hour ranges -> bands most likely open from mid-latitude North America (best first)
    (range(0, 6), ["80", "40", "160"]), (range(6, 9), ["40", "80", "20"]),
    (range(9, 16), ["20", "17", "15", "40", "12", "10"]), (range(16, 19), ["20", "40", "17", "15"]),
    (range(19, 24), ["40", "80", "20"]),
]
DEFAULT_ANTENNA = {"name": "HF antenna", "bands": ["80", "40", "20", "17", "15", "12", "10"]}
STEP_KHZ = 3.0
SETTLE_S = 0.25
SIGNAL_OVER = 26          # S-meter counts above the floor (about 2 S-units) = worth a listen
DWELL_S = 30
STAY_QUIET_S = 120
CANDIDATES = 4
PRE_NET_MIN = 5
QUIET_MIN = 10
HOLD_MIN = 30
TICK_S = 5


def band_of(f: float) -> str | None:
    for b, (a, z, _) in BANDS.items():
        if a <= f <= z:
            return b
    return None


def bands_now(hour: int, antenna_bands) -> list[str]:
    for hrs, bs in BY_HOUR:
        if hour in hrs:
            return [b for b in bs if b in antenna_bands]
    return []


def candidates(levels: list[tuple[float, int]], over=SIGNAL_OVER, n=CANDIDATES) -> list[tuple[float, int]]:
    """Peaks above the floor (median), one per 6 kHz, strongest first."""
    if len(levels) < 3:
        return []
    floor = statistics.median(v for _, v in levels)
    peaks = [(f, v) for i, (f, v) in enumerate(levels) if v >= floor + over
             and v >= max(levels[i - 1][1] if i else 0, levels[i + 1][1] if i + 1 < len(levels) else 0)]
    out = []
    for f, v in sorted(peaks, key=lambda x: -x[1]):
        if all(abs(f - g) > 0.006 for g, _ in out):
            out.append((f, v))
    return out[:n]


class Spots:
    """Frequencies where conversations were heard, ranked for a given hour."""

    def __init__(self, path):
        self.path = path
        try:
            self.d = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            self.d = {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def key(f, mode):
        return f"{f:.3f} {mode}"

    def hit(self, f, mode, at: datetime, calls=()):
        k = self.key(f, mode)
        s = self.d.setdefault(k, {"freq_mhz": round(f, 3), "mode": mode, "band": band_of(f), "hits": 0,
                                  "hours": {}, "days": {}, "calls": [], "pinned": False, "hidden": False})
        s["hits"] += 1
        s["hours"][str(at.hour)] = s["hours"].get(str(at.hour), 0) + 1
        s["days"][at.strftime("%a")] = s["days"].get(at.strftime("%a"), 0) + 1
        s["last"] = at.isoformat(timespec="minutes")
        s["calls"] = (list(dict.fromkeys(list(calls) + s["calls"])))[:12]
        self.save()

    def score(self, s, hour) -> float:
        h = s.get("hours") or {}
        near = sum(h.get(str((hour + d) % 24), 0) * w for d, w in ((0, 1.0), (-1, 0.5), (1, 0.5)))
        return (5 if s.get("pinned") else 0) + near + 0.2 * s.get("hits", 0)

    def ranked(self, hour, band=None) -> list[dict]:
        L = [s | {"key": k, "score": round(self.score(s, hour), 2)} for k, s in self.d.items()
             if not s.get("hidden") and (band is None or s.get("band") == band)]
        return sorted(L, key=lambda s: -s["score"])

    def act(self, key, what):
        if key not in self.d:
            raise ValueError("no such spot")
        if what == "forget":
            self.d.pop(key)
        elif what in ("pin", "unpin"):
            self.d[key]["pinned"] = what == "pin"
        elif what in ("hide", "unhide"):
            self.d[key]["hidden"] = what == "hide"
        else:
            raise ValueError("pin, unpin, hide, unhide or forget")
        self.save()


class Scanner:
    def __init__(self, station):
        self.st = station
        self.cfg_path = station.data / "scan.json"
        try:
            self.cfg = json.loads(self.cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            self.cfg = {}
        self.cfg.setdefault("radios", {})
        self.spots = Spots(station.data / "scan_spots.json")
        self.state: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        for rid in station.radios:
            self._show(rid)

    # ----- settings -----
    def _save(self):
        tmp = self.cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")
        os.replace(tmp, self.cfg_path)

    def rc(self, rid) -> dict:
        return self.cfg["radios"].setdefault(rid, {"mode": "off", "antenna": dict(DEFAULT_ANTENNA)})

    def can(self, rid) -> str | None:
        """Why this radio can't scan, or None."""
        r = self.st.radios.get(rid) or {}
        cat = getattr(self.st, "cat", None)
        if cat is None or not cat.has(rid):
            return "the scan needs CAT (Radios… → CAT port)"
        if r.get("role") == "winlink":
            return "kept for Winlink"
        if r.get("antenna") == "vu":
            return "its VHF/UHF antenna is on (the scan is for HF)"
        if (r.get("model") or "") not in ("ic705", "hf", "any"):
            return "not an HF radio"
        return None

    def set_mode(self, rid, mode) -> dict:
        if rid not in self.st.radios:
            raise KeyError(rid)
        if mode not in ("off", "on", "auto"):
            raise ValueError("scan: off, on or auto")
        if mode != "off" and self.can(rid):
            raise ValueError(f"can't scan: {self.can(rid)}")
        with self.lock:
            self.rc(rid)["mode"] = mode
            self._save()
        s = self.state.setdefault(rid, {})
        s.pop("held_until", None)
        if mode == "off":
            s["stop"] = True
        self._show(rid, say="Scan off" if mode == "off" else "Scan on: starts when no net is on")
        return self.view()

    def set_antenna(self, rid, body) -> dict:
        if rid not in self.st.radios:
            raise KeyError(rid)
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:40] or DEFAULT_ANTENNA["name"]
        bands = [b for b in (body.get("bands") or []) if b in BANDS]
        if not bands:
            raise ValueError("choose at least one band the antenna works on")
        with self.lock:
            self.rc(rid)["antenna"] = {"name": name, "bands": bands}
            self._save()
        self._show(rid)
        return self.view()

    def view(self) -> dict:
        h = self.st.clock().hour
        return {"radios": {rid: self.rc(rid) | {"can": self.can(rid), "state": self.state.get(rid, {}).get("show")}
                           for rid in self.st.radios},
                "bands": {b: {"from": a, "to": z, "mode": m} for b, (a, z, m) in BANDS.items()},
                "now": bands_now(h, list(BANDS)), "spots": self.spots.ranked(h)[:40]}

    def post(self, path, body) -> dict:
        """POST /api/scan/<rid> {"mode"}, /api/scan/<rid>/antenna {"name", "bands"}, /api/scan/spot {"key", "do"}."""
        m = re.fullmatch(r"/api/scan/([\w-]+)(?:/(antenna))?", path)
        if not m:
            raise ValueError("unknown scan request")
        if m.group(1) == "spot":
            self.spots.act(str(body.get("key")), str(body.get("do")))
            return self.view()
        if m.group(2) == "antenna":
            return self.set_antenna(m.group(1), body)
        return self.set_mode(m.group(1), str(body.get("mode") or "off"))

    # ----- the card -----
    def _show(self, rid, **kw):
        r = self.st.radios.get(rid)
        if r is None:
            return
        rc = self.rc(rid)
        show = {"mode": rc["mode"], "antenna": rc["antenna"], "can": self.can(rid)} | kw
        old = r.get("scan")
        r["scan"] = show
        self.state.setdefault(rid, {})["show"] = show
        if old != show:
            try:
                self.st.bus.publish("radios", {"radios": list(self.st.radios.values()), "net": self.st.schedule_now()})
            except Exception:  # noqa: BLE001
                pass

    # ----- may it scan now? -----
    def allowed(self, rid, at: datetime) -> tuple[bool, str]:
        rc, r = self.rc(rid), self.st.radios.get(rid) or {}
        if rc["mode"] == "off":
            return False, "Scan off"
        why = self.can(rid)
        if why:
            return False, f"Can't scan: {why}"
        s = self.state.setdefault(rid, {})
        if s.get("held_until") and at < s["held_until"]:
            return False, f"You tuned the radio: scan waits until {s['held_until']:%H:%M}"
        if r.get("paused") and r.get("paused_by") == "operator":
            return False, "Paused by you"
        L = r.get("listen") or {}
        nxt = L.get("next")
        if nxt and datetime.fromisoformat(nxt["start"]) - at <= timedelta(minutes=PRE_NET_MIN):
            return False, f"{nxt['name']} starts at {datetime.fromisoformat(nxt['start']):%H:%M}: the net first"
        if L.get("now"):
            if rc["mode"] != "auto":
                return False, f"{L['now']} is on: the net first"
            q = (getattr(self.st.nets, "_quiet", {}) or {}).get(rid) or {}
            since = q.get("since")
            if not since or at - since < timedelta(minutes=QUIET_MIN) or self.st.nets._heard_since(rid, since):
                return False, f"{L['now']} is on: the net first (Auto scans if it stays quiet {QUIET_MIN} min)"
        return True, ""

    # ----- the loop -----
    def run(self, stop: threading.Event):
        while not stop.wait(TICK_S):
            for rid in list(self.st.radios):
                try:
                    ok, why = self.allowed(rid, self.st.clock())
                    if ok:
                        self.cycle(rid)
                    else:
                        self._show(rid, state="idle", say=why)
                        self._end(rid)
                except Exception as e:  # noqa: BLE001 - the scan never stops the station
                    print(f"{rid} scan: {type(e).__name__}: {e}", flush=True)
                    self._show(rid, state="error", say=f"Scan problem: {e}")
                    self._end(rid)

    def _rig(self, rid):
        return self.st.cat._rig(rid)

    def _begin(self, rid, rig):
        s = self.state.setdefault(rid, {})
        if not s.get("active"):
            r = self.st.radios[rid]
            s.update(active=True, restore=(r.get("freq_mhz"), r.get("mode")), resumed=bool(r.get("paused")),
                     stop=False)
            if r.get("paused"):
                self.st.set_paused(rid, False, by="scan")

    def _end(self, rid):
        s = self.state.get(rid) or {}
        if not s.get("active"):
            return
        s["active"] = False
        self.st.cat.hold.discard(rid)
        r = self.st.radios.get(rid) or {}
        f, m = s.get("restore") or (None, None)
        L = r.get("listen") or {}
        if f and not L.get("now") and not s.get("held_until"):
            try:
                self.st.cat.tune_now(rid, f, m)                 # back where the radio was before the scan
            except Exception:  # noqa: BLE001
                pass
        if s.get("resumed") and not L.get("now") and not r.get("paused"):
            self.st.set_paused(rid, True, by="scan")

    def _still(self, rid) -> bool:
        s = self.state.get(rid) or {}
        if s.get("stop"):
            s["stop"] = False
            return False
        return self.allowed(rid, self.st.clock())[0]

    def _touched(self, rid, rig, expect) -> bool:
        """The operator turned the dial: the radio is not where the scan put it."""
        try:
            f = rig.frequency()
        except Exception:  # noqa: BLE001
            return False
        if expect and abs(f - expect) > 0.0002:
            s = self.state.setdefault(rid, {})
            s["held_until"] = self.st.clock() + timedelta(minutes=HOLD_MIN)
            s["restore"] = (None, None)
            self.st.cat.hold.discard(rid)
            self.st.set_radio(rid, round(f, 4), rig.mode())
            self._show(rid, state="held", say=f"You tuned to {f:.3f}: scan waits {HOLD_MIN} min")
            return True
        return False

    def cycle(self, rid):
        rig = self._rig(rid)
        if rig is None:
            self._show(rid, state="error", say="CAT not answering")
            return
        self._begin(rid, rig)
        s = self.state[rid]
        at = self.st.clock()
        ant = self.rc(rid)["antenna"]["bands"]
        bands = bands_now(at.hour, ant) or [b for b in ant if b in BANDS]
        if not bands:
            self._show(rid, state="idle", say="No band from the antenna's list is open now")
            return
        i = s.get("next_band", 0) % len(bands)
        s["next_band"] = i + 1
        b = bands[i]
        lo, hi, mode = BANDS[b]
        tries = [(sp["freq_mhz"], None, "remembered") for sp in self.spots.ranked(at.hour, b)[:3] if sp["score"] >= 1]
        self.st.cat.hold.add(rid)
        try:
            rig.tune(lo, mode)
            expect = lo
            levels = []
            self._show(rid, state="sweeping", band=b, say=f"Scanning {b} m ({lo:.3f}–{hi:.3f} {mode})")
            f = lo
            while f <= hi + 1e-9:
                if not self._still(rid) or self._touched(rid, rig, expect):
                    return
                rig.set_freq(f)
                expect = f
                time.sleep(SETTLE_S)
                levels.append((round(f, 4), rig.smeter()))
                f += STEP_KHZ / 1000
            tries += [(f, v, "signal") for f, v in candidates(levels)
                      if all(abs(f - t[0]) > 0.004 for t in tries)]
            s["last_sweep"] = {"band": b, "floor": statistics.median(v for _, v in levels) if levels else None,
                               "peaks": len(tries)}
        finally:
            self.st.cat.hold.discard(rid)
        if not tries:
            self._show(rid, state="sweeping", band=b, say=f"{b} m: nothing above the noise; next band")
            return
        for f, v, why in tries:
            if not self._still(rid):
                return
            if self.listen_on(rid, rig, f, mode, why):
                return                                              # a conversation ran its course: new cycle

    def listen_on(self, rid, rig, f, mode, why) -> bool:
        """Park on f for DWELL_S; a conversation (speech transcribed, not noise) keeps it there and is remembered."""
        r = self.st.radios[rid]
        if r.get("paused") and r.get("paused_by") != "operator":     # the schedule paused it after a net
            self.st.set_paused(rid, False, by="scan")
            self.state[rid]["resumed"] = True
        self.st.cat.tune_now(rid, round(f, 4), mode)
        parked = self.st.clock()
        self._show(rid, state="listening", freq=round(f, 4), mode=mode,
                   say=f"Listening on {f:.3f} {mode} ({'a spot heard before' if why == 'remembered' else 'a signal'})")
        found, last = False, None
        end = time.monotonic() + DWELL_S
        while True:
            if not self._still(rid) or self._touched(rid, rig, f):
                return found
            heard = [t for t in list(self.st.tx) if t.get("radio") == rid
                     and str(t.get("start") or "") >= parked.isoformat(timespec="seconds")]
            talk = [t for t in heard if t.get("status") == "final" and not t.get("noise")
                    and len((t.get("text") or "").split()) >= 3]
            if talk:
                newest = max(datetime.fromisoformat(t["start"]) for t in talk)
                if not found:
                    found = True
                    calls = [c for t in talk for c in (t.get("callsigns") or [])]
                    self.spots.hit(f, mode, self.st.clock(), calls)
                if last is None or newest > last:
                    last = newest
                    cs = sorted({c for t in talk for c in (t.get("callsigns") or [])})
                    self._show(rid, state="conversation", freq=round(f, 4), mode=mode,
                               say=f"Conversation on {f:.3f} {mode}" + (f": {', '.join(cs[:4])}" if cs else ""))
                if self.st.clock() - last > timedelta(seconds=STAY_QUIET_S):
                    return True
            elif time.monotonic() > end and not any(t.get("status") != "final" for t in heard):
                return found
            time.sleep(1)
