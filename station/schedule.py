"""The net schedule, edited on the dashboard, "listen only during scheduled nets" per radio, and the pre-net alert.

The schedule starts as config/station.yaml's `schedule:` entries. The first edit on the dashboard writes
data/schedule.json, and from then on that file is the schedule (station.yaml is never rewritten; delete
schedule.json to start again from station.yaml). No restart is needed: Station.cfg["schedule"] is replaced,
so schedule_now() and the radio cards see the change at once, and every open browser gets it over the
event stream ("schedule").

When a net meets is an Outlook-style rule (station/recur.py: once / daily / weekly / monthly on day N or the
Nth weekday / yearly, start, end on a date or after N times) plus exceptions (one occurrence cancelled or moved).
Old entries (`days: [Tue, Thu]`) are read as weekly rules; the new form is written on the next edit.

Listen only during scheduled nets (per radio, kept in data/operator.json beside `paused`): the ticker
resumes the radio LEAD_MIN before a net on it starts and pauses it GRACE_MIN after it ends, through
Station.set_paused(by="schedule"). It acts only when the wanted state changes (a net window opens or
closes), so a Pause or Resume pressed by the operator holds until the next start or end. Optionally the
radio's logged frequency/mode is set to the net's when the net starts (the operator still tunes the radio).
An occurrence skipped from its alert ("Skip this net") is not listened to.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

from . import recur

DAYS = recur.DAYS
MODES = ("FM", "USB", "LSB", "AM", "CW", "DIG")
LEAD_MIN = 1          # resume this many minutes before a net starts (owner 11-02: one minute)
GRACE_MIN = 5         # pause this many minutes after it ends (nets run over)
ALERT_MIN = 5         # the pre-net alert, this many minutes before (per net: alert_min; 0 = no alert)
BELONGS_MIN = 15      # a transmission this close to an occurrence belongs to that net's history
LOOK_AHEAD_DAYS = 62  # "paused until ..." looks this far ahead (a monthly net)
CALL = re.compile(r"[A-Z0-9]{3,8}(-\d{1,2})?")


class NoSuchNet(LookupError):
    pass


def validate(body: dict, radios: dict) -> dict:
    """One schedule entry from the dashboard (or station.yaml), checked and normalized. Raises ValueError.
    The old weekly form (days: [Tue]) is accepted and becomes repeat: {freq: weekly, weekdays: [Tue]}."""
    if not isinstance(body, dict):
        raise ValueError("a net must be a JSON object")
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("give the net a name")
    if len(name) > 80:
        raise ValueError("the net's name is too long (80 characters at most)")
    rid = str(body.get("radio") or "").strip()
    if rid not in radios:
        raise ValueError(f"no radio called {rid!r}" if rid else "choose a radio")
    start = recur.hhmm(body.get("start"))
    try:
        dur = int(str(body.get("duration_min", 60)).strip())
    except ValueError:
        raise ValueError("duration must be a whole number of minutes") from None
    if not 1 <= dur <= 1440:
        raise ValueError("duration must be 1 to 1440 minutes")
    f = body.get("freq_mhz")
    if f in (None, ""):
        f = None
    else:
        try:
            f = round(float(str(f).replace(",", ".")), 4)
        except ValueError:
            raise ValueError("frequency must be a number in MHz, like 146.940") from None
        if not 0.1 <= f <= 3000:
            raise ValueError("frequency must be between 0.1 and 3000 MHz")
    mode = str(body.get("mode") or "").strip().upper() or None
    if mode and mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    ncs = str(body.get("ncs") or "").strip().upper() or None
    if ncs and not CALL.fullmatch(ncs):
        raise ValueError("net control must be a callsign, like K7ABC")
    if not body.get("repeat") and "days" in body and body["days"] in (None, "", []):
        raise ValueError("choose at least one day")
    rule = recur.normalize(body.get("repeat"), body.get("days"))
    alert = body.get("alert_min")
    try:
        alert = ALERT_MIN if alert in (None, "") else int(str(alert).strip())
    except ValueError:
        raise ValueError("the alert must be a whole number of minutes (0 = no alert)") from None
    if not 0 <= alert <= 120:
        raise ValueError("the alert must be 0 to 120 minutes before the net")
    out = {"name": name, "radio": rid, "freq_mhz": f, "mode": mode, "start": start, "duration_min": dur, "ncs": ncs,
           "repeat": rule, "exceptions": recur.normalize_exceptions(body.get("exceptions"), MODES) or None,
           "alert_min": alert, "emergency": True if body.get("emergency") else None}
    return {k: v for k, v in out.items() if v is not None}


def occurrences(net: dict, at: datetime, back_days=1, ahead_days=8):
    """(start, end) of each time the net runs from `back_days` before `at` to `ahead_days` after it."""
    for o in recur.occurrences(net, at - timedelta(days=back_days), at + timedelta(days=ahead_days)):
        yield o["start"], o["end"]


def item(n: dict, o: dict) -> dict:
    """One occurrence as the dashboard, the alerts and the history see it."""
    return {"id": n.get("id"), "name": n.get("name"), "radio": n.get("radio"), "date": o["date"],
            "key": recur.key(n.get("id"), o["date"]), "start": o["start"].isoformat(timespec="minutes"),
            "end": o["end"].isoformat(timespec="minutes"), "freq_mhz": o.get("freq_mhz"), "mode": o.get("mode"),
            "ncs": n.get("ncs"), "note": o.get("note"), "moved": o.get("moved", False),
            "emergency": bool(n.get("emergency"))}


def listen_window(nets: list[dict], rid: str, at: datetime, lead=LEAD_MIN, grace=GRACE_MIN, skip=()):
    """(the net whose listening window [start - lead, end + grace) holds `at`, or None;
    the next net on this radio whose window opens after `at`, or None). Pure: the ticker and tests use it.
    `skip`: occurrence keys the operator chose to skip (not listened to)."""
    cur, nxt = None, None
    for n in nets:
        if n.get("radio") != rid:
            continue
        for o in recur.occurrences(n, at - timedelta(days=1), at + timedelta(days=LOOK_AHEAD_DAYS)):
            it = item(n, o)
            if it["key"] in skip:
                continue
            s, e = o["start"], o["end"]
            if s - timedelta(minutes=lead) <= at < e + timedelta(minutes=grace):
                if cur is None or it["start"] > cur["start"]:        # back-to-back: the one starting now
                    cur = it
            elif s - timedelta(minutes=lead) > at and (nxt is None or it["start"] < nxt["start"]):
                nxt = it
    return cur, nxt


def occurrence_at(nets: list[dict], rid: str, at: datetime, before=BELONGS_MIN, after=BELONGS_MIN) -> dict | None:
    """The occurrence on radio `rid` a transmission at `at` belongs to: from `before` minutes before it starts to
    `after` minutes after it ends (the nearest start wins when two overlap). None = not a scheduled net."""
    best = None
    for n in nets:
        if n.get("radio") != rid:
            continue
        for o in recur.occurrences(n, at - timedelta(days=1), at + timedelta(days=1)):
            if o["start"] - timedelta(minutes=before) <= at < o["end"] + timedelta(minutes=after):
                d = abs((at - o["start"]).total_seconds())
                if best is None or d < best[0]:
                    best = (d, item(n, o))
    return best[1] if best else None


def running(nets: list[dict], at: datetime) -> list[dict]:
    """Every occurrence on the air at `at` (start <= at < end), any radio, earliest start first."""
    out = []
    for n in nets:
        for o in recur.occurrences(n, at - timedelta(days=1), at + timedelta(minutes=1)):
            if o["start"] <= at < o["end"]:
                out.append(item(n, o))
    return sorted(out, key=lambda x: x["start"])


def preview(net: dict, at: datetime, n=5) -> dict:
    """The editor's preview: the rule in plain words and the next `n` occurrences (cancelled ones marked)."""
    rows = [{"date": o["date"], "start": o["start"].isoformat(timespec="minutes"),
             "end": o["end"].isoformat(timespec="minutes"), "freq_mhz": o.get("freq_mhz"), "mode": o.get("mode"),
             "moved": o.get("moved"), "cancelled": bool(o.get("cancelled")), "note": o.get("note")}
            for o in recur.upcoming(net, at, n, cancelled=True)]
    return {"summary": recur.summary(net), "next": rows, "ended": not any(not r["cancelled"] for r in rows)}


class NetSchedule:
    def __init__(self, station):
        self.st = station
        self.lock = threading.RLock()
        self._want: dict[str, str | None] = {}    # radio -> the net window wanted at the last tick ("" = none)
        self.st.cfg["schedule"] = self._load()
        self._apply_saved()
        from .alerts import Alerts
        self.alerts = Alerts(station, self)

    # ---------- storage ----------
    @property
    def path(self):
        return self.st.data / "schedule.json"

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")).get("nets") or []
        except (OSError, ValueError, AttributeError):
            raw = self.st.cfg.get("schedule") or []           # not edited yet: station.yaml's entries
        nets = []
        for i, n in enumerate(raw):
            try:
                v = validate(n, self.st.radios)               # an old `days:` entry becomes a weekly rule here
            except ValueError as e:
                print(f"schedule: entry {n.get('name') if isinstance(n, dict) else n!r} skipped: {e}", flush=True)
                continue
            nets.append({"id": str(n.get("id") or f"n{i + 1}")} | v)
        return nets

    def _commit(self, nets: list[dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"nets": nets}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        self.st.cfg["schedule"] = nets
        self.tick()
        self.publish()

    def decorated(self, at: datetime | None = None) -> list[dict]:
        """The nets with what the dashboard shows beside each: the rule in words and the next time it meets."""
        at = at or self.st.clock()
        out = []
        for n in self.st.cfg.get("schedule") or []:
            nxt = [o for o in recur.upcoming(n, at, 2) if o["start"] > at][:1]      # the next start (not the one on now)
            out.append(n | {"summary": recur.summary(n),
                            "next": item(n, nxt[0]) if nxt else None})
        return out

    def publish(self):
        self.st.bus.publish("schedule", {"schedule": self.decorated(), "net": self.st.schedule_now()})

    def list(self) -> dict:
        return {"schedule": self.decorated(), "edited": self.path.exists(), "days": DAYS, "modes": list(MODES),
                "radios": [{"id": r["id"], "name": r["name"], "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode"),
                            "listen": r.get("listen")} for r in self.st.radios.values()],
                "lead_min": LEAD_MIN, "grace_min": GRACE_MIN, "alert_min": ALERT_MIN}

    def _find(self, nid: str) -> tuple:
        nets = [dict(n) for n in self.st.cfg["schedule"]]
        i = next((i for i, n in enumerate(nets) if n["id"] == nid), None)
        if i is None:
            raise NoSuchNet(nid)
        return nets, i

    def add(self, body: dict) -> dict:
        v = validate(body, self.st.radios)
        with self.lock:
            nets = list(self.st.cfg["schedule"])
            ids = {n["id"] for n in nets}
            nid = "n" + uuid.uuid4().hex[:6]
            while nid in ids:
                nid = "n" + uuid.uuid4().hex[:6]
            nets.append({"id": nid} | v)
            self._commit(nets)
        return self.list() | {"id": nid}

    def update(self, nid: str, body: dict) -> dict:
        with self.lock:
            nets, i = self._find(nid)
            if "exceptions" not in body and nets[i].get("exceptions"):
                body = body | {"exceptions": nets[i]["exceptions"]}       # the editor does not resend them
            nets[i] = {"id": nid} | validate(body, self.st.radios)
            self._commit(nets)
        return self.list()

    def delete(self, nid: str) -> dict:
        with self.lock:
            nets = [n for n in self.st.cfg["schedule"] if n["id"] != nid]
            if len(nets) == len(self.st.cfg["schedule"]):
                raise NoSuchNet(nid)
            self._commit(nets)
        return self.list()

    def except_one(self, nid: str, body: dict, what: str) -> dict:
        """Cancel ("skip"), move/change ("move") or put back ("restore") one occurrence, by its own date."""
        occ = recur._date(body.get("date"), "the occurrence")
        if not occ:
            raise ValueError("say which occurrence (its date)")
        with self.lock:
            nets, i = self._find(nid)
            n = nets[i]
            ex = dict(n.get("exceptions") or {})
            if what == "restore":
                ex.pop(occ, None)
            else:
                if not recur.matches(recur.rule_of(n), datetime.fromisoformat(occ).date()):
                    raise ValueError(f"{n['name']} does not meet on {occ}")
                if what == "skip":
                    ex[occ] = {"cancel": True} | ({"note": body["note"]} if body.get("note") else {})
                else:
                    ex[occ] = {k: body[k] for k in ("date", "start", "duration_min", "freq_mhz", "mode", "note")
                               if body.get(k) not in (None, "")}
            n["exceptions"] = recur.normalize_exceptions(ex, MODES)
            if not n["exceptions"]:
                n.pop("exceptions")
            nets[i] = {"id": nid} | validate(n, self.st.radios)
            self._commit(nets)
        return self.list()

    def preview(self, body: dict) -> dict:
        """The editor's live preview of a net being typed (nothing saved)."""
        v = validate(body, self.st.radios)
        return preview(v, self.st.clock())

    def post(self, path: str, body: dict) -> dict:
        """POST /api/schedule (add), /api/schedule/preview, /api/schedule/<id> (replace), /api/schedule/<id>/delete,
        /api/schedule/<id>/skip|move|restore ({"date": the occurrence's own date, ...})."""
        m = re.fullmatch(r"/api/schedule(?:/([\w-]+)(?:/(delete|skip|move|restore))?)?", path)
        if not m:
            raise NoSuchNet(path)
        if not m.group(1):
            return self.add(body)
        if m.group(1) == "preview" and not m.group(2):
            return self.preview(body)
        if m.group(2) == "delete":
            return self.delete(m.group(1))
        if m.group(2):
            return self.except_one(m.group(1), body, m.group(2))
        return self.update(m.group(1), body)

    # ---------- listen only during scheduled nets ----------
    def _apply_saved(self):
        saved = self.st._saved().get("radios") or {}
        for rid, r in self.st.radios.items():
            s = saved.get(rid) or {}
            r["listen"] = {"scheduled": bool(s.get("listen_scheduled")), "tune": bool(s.get("tune_to_net")),
                           "now": None, "next": None}
            r["paused_by"] = (s.get("paused_by") or "operator") if r.get("paused") else None

    def set_listen(self, rid: str, body: dict) -> dict:
        """Turn "listen only during scheduled nets" (and "set the frequency to the net's") on or off."""
        if rid not in self.st.radios:
            raise KeyError(rid)
        with self.st.lock:
            L = self.st.radios[rid]["listen"]
            if "scheduled" in body:
                L["scheduled"] = bool(body["scheduled"])
            if "tune" in body:
                L["tune"] = bool(body["tune"])
            saved = self.st._saved()
            saved.setdefault("radios", {}).setdefault(rid, {}).update(listen_scheduled=L["scheduled"],
                                                                     tune_to_net=L["tune"])
            self.st._save(saved)
        self._want[rid] = None                        # decide afresh at once (as a real start/end, not a restart)
        self.tick()
        self.st.bus.publish("radios", {"radios": list(self.st.radios.values()), "net": self.st.schedule_now()})
        return dict(self.st.radios[rid])

    def tick(self, at: datetime | None = None):
        """Called every second by the station's ticker: the pre-net alerts, and resume/pause radios at their nets."""
        at = at or self.st.clock()
        nets = self.st.cfg.get("schedule") or []
        try:
            self.alerts.tick(at)
        except Exception as e:  # noqa: BLE001 — an alert problem never stops listening
            print(f"alerts: {type(e).__name__}: {e}", flush=True)
        skip = self.alerts.skipped()
        with self.lock:
            for rid, r in list(self.st.radios.items()):
                L = r.setdefault("listen", {"scheduled": False, "tune": False})
                cur, nxt = listen_window(nets, rid, at, skip=skip)
                L["now"] = cur["name"] if cur else None
                L["next"] = {"name": nxt["name"], "start": nxt["start"]} if nxt else None
                if not L.get("scheduled"):
                    self._want.pop(rid, None)
                    continue
                want = cur["key"] if cur else ""                       # which net window (edge = a change)
                first = rid not in self._want
                if self._want.get(rid) == want:
                    continue
                self._want[rid] = want
                if want:
                    # a restart inside a net keeps a pause the operator chose; a net's start always resumes
                    if r.get("paused") and not (first and r.get("paused_by") == "operator"):
                        self.st.set_paused(rid, False, by="schedule")
                    f, mode = cur.get("freq_mhz"), cur.get("mode")
                    if L.get("tune") and f and (abs(f - (r.get("freq_mhz") or 0)) > 0.0005
                                                or (mode and mode != r.get("mode"))):
                        self.st.set_radio(rid, f, mode)
                        print(f"{datetime.now():%H:%M:%S} {rid} logged as {f} MHz {mode or ''} for {cur['name']}",
                              flush=True)
                elif not r.get("paused"):
                    self.st.set_paused(rid, True, by="schedule")
