"""The net schedule, edited on the dashboard, and "listen only during scheduled nets" per radio.

The schedule starts as config/station.yaml's `schedule:` entries. The first edit on the dashboard writes
data/schedule.json, and from then on that file is the schedule (station.yaml is never rewritten; delete
schedule.json to start again from station.yaml). No restart is needed: Station.cfg["schedule"] is replaced,
so schedule_now() and the radio cards see the change at once, and every open browser gets it over the
event stream ("schedule").

Listen only during scheduled nets (per radio, kept in data/operator.json beside `paused`): the ticker
resumes the radio LEAD_MIN before a net on it starts and pauses it GRACE_MIN after it ends, through
Station.set_paused(by="schedule"). It acts only when the wanted state changes (a net window opens or
closes), so a Pause or Resume pressed by the operator holds until the next start or end. Optionally the
radio's logged frequency/mode is set to the net's when the net starts (the operator still tunes the radio).
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MODES = ("FM", "USB", "LSB", "AM", "CW", "DIG")
LEAD_MIN = 1          # resume this many minutes before a net starts (owner 11-02: one minute)
GRACE_MIN = 5         # pause this many minutes after it ends (nets run over)
CALL = re.compile(r"[A-Z0-9]{3,8}(-\d{1,2})?")


class NoSuchNet(LookupError):
    pass


def _days(v) -> list[str]:
    if v in (None, "", []):
        raise ValueError("choose at least one day")
    if isinstance(v, str):
        v = re.split(r"[,\s]+", v.strip())
    if not isinstance(v, (list, tuple)):
        raise ValueError("days must be a list like [Tue, Thu]")
    out = set()
    for d in v:
        d = str(d).strip()[:3].title()
        if d not in DAYS:
            raise ValueError(f"days must be Mon, Tue, Wed, Thu, Fri, Sat or Sun (got {d or 'nothing'})")
        out.add(d)
    return [d for d in DAYS if d in out]


def validate(body: dict, radios: dict) -> dict:
    """One schedule entry from the dashboard (or station.yaml), checked and normalized. Raises ValueError."""
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
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(body.get("start") or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("start must be a time like 20:00 (24-hour clock)")
    start = f"{int(m.group(1)):02d}:{m.group(2)}"
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
        raise ValueError("net control must be a callsign, like WA7XQZ")
    out = {"name": name, "radio": rid, "freq_mhz": f, "mode": mode, "days": _days(body.get("days", DAYS)),
           "start": start, "duration_min": dur, "ncs": ncs}
    return {k: v for k, v in out.items() if v is not None}


def occurrences(net: dict, at: datetime, back_days=1, ahead_days=8):
    """(start, end) of each time the net runs from `back_days` before `at` to `ahead_days` after it."""
    days = [DAYS.index(str(d)[:3].title()) for d in net.get("days", DAYS)]
    hh, mm = map(int, str(net.get("start", "00:00")).split(":"))
    for add in range(-back_days, ahead_days):
        d = (at + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d.weekday() in days:
            yield d, d + timedelta(minutes=int(net.get("duration_min", 60)))


def listen_window(nets: list[dict], rid: str, at: datetime, lead=LEAD_MIN, grace=GRACE_MIN):
    """(the net whose listening window [start - lead, end + grace) holds `at`, or None;
    the next net on this radio whose window opens after `at`, or None). Pure: the ticker and tests use it."""
    cur, nxt = None, None
    for n in nets:
        if n.get("radio") != rid:
            continue
        for s, e in occurrences(n, at):
            item = {"id": n.get("id"), "name": n.get("name"), "start": s.isoformat(timespec="minutes"),
                    "end": e.isoformat(timespec="minutes"), "freq_mhz": n.get("freq_mhz"), "mode": n.get("mode")}
            if s - timedelta(minutes=lead) <= at < e + timedelta(minutes=grace):
                if cur is None or item["start"] > cur["start"]:      # back-to-back: the one starting now
                    cur = item
            elif s - timedelta(minutes=lead) > at and (nxt is None or item["start"] < nxt["start"]):
                nxt = item
    return cur, nxt


class NetSchedule:
    def __init__(self, station):
        self.st = station
        self.lock = threading.RLock()
        self._want: dict[str, str | None] = {}    # radio -> the net window wanted at the last tick ("" = none)
        self.st.cfg["schedule"] = self._load()
        self._apply_saved()

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
                v = validate(n, self.st.radios)
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

    def publish(self):
        self.st.bus.publish("schedule", {"schedule": self.st.cfg["schedule"], "net": self.st.schedule_now()})

    def list(self) -> dict:
        return {"schedule": self.st.cfg["schedule"], "edited": self.path.exists(), "days": DAYS, "modes": list(MODES),
                "radios": [{"id": r["id"], "name": r["name"], "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode"),
                            "listen": r.get("listen")} for r in self.st.radios.values()],
                "lead_min": LEAD_MIN, "grace_min": GRACE_MIN}

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
        v = validate(body, self.st.radios)
        with self.lock:
            nets = list(self.st.cfg["schedule"])
            i = next((i for i, n in enumerate(nets) if n["id"] == nid), None)
            if i is None:
                raise NoSuchNet(nid)
            nets[i] = {"id": nid} | v
            self._commit(nets)
        return self.list()

    def delete(self, nid: str) -> dict:
        with self.lock:
            nets = [n for n in self.st.cfg["schedule"] if n["id"] != nid]
            if len(nets) == len(self.st.cfg["schedule"]):
                raise NoSuchNet(nid)
            self._commit(nets)
        return self.list()

    def post(self, path: str, body: dict) -> dict:
        """POST /api/schedule (add), /api/schedule/<id> (replace), /api/schedule/<id>/delete."""
        m = re.fullmatch(r"/api/schedule(?:/([\w-]+)(/delete)?)?", path)
        if not m:
            raise NoSuchNet(path)
        if not m.group(1):
            return self.add(body)
        return self.delete(m.group(1)) if m.group(2) else self.update(m.group(1), body)

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
        """Called every second by the station's ticker: resume/pause radios at their nets' windows."""
        at = at or self.st.clock()
        nets = self.st.cfg.get("schedule") or []
        with self.lock:
            for rid, r in list(self.st.radios.items()):
                L = r.setdefault("listen", {"scheduled": False, "tune": False})
                cur, nxt = listen_window(nets, rid, at)
                L["now"] = cur["name"] if cur else None
                L["next"] = {"name": nxt["name"], "start": nxt["start"]} if nxt else None
                if not L.get("scheduled"):
                    self._want.pop(rid, None)
                    continue
                want = f"{cur['id']}@{cur['start']}" if cur else ""      # which net window (edge = a change)
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
