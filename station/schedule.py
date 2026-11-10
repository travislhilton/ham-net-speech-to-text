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
Without that switch a scheduled net is still always recorded: a radio paused by hand is resumed for the net and
paused again after it (unless the operator pressed Pause or Resume during the net).
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

from . import fit, recur

DAYS = recur.DAYS
MODES = ("FM", "USB", "LSB", "AM", "CW", "DIG")
LEAD_MIN = 1          # resume this many minutes before a net starts (owner 11-02: one minute)
GRACE_MIN = 5         # pause this many minutes after it ends (nets run over)
OVERRUN_QUIET_MIN = 5   # a net past its scheduled end keeps running while it was heard less than this long ago
OVERRUN_MAX_MIN = 180   # ... but never longer than this past its scheduled end
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
    rid = str(body.get("radio") or "").strip() or None            # no radio: kept, not recorded (station/fit.py)
    if rid is not None and rid not in radios:
        raise ValueError(f"no radio called {rid!r}")
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
    def num(k, lo, hi, what):
        v = body.get(k)
        if v in (None, ""):
            return None
        try:
            v = round(float(str(v).replace(",", ".")), 4)
        except ValueError:
            raise ValueError(f"{what} must be a number") from None
        if not lo <= v <= hi:
            raise ValueError(f"{what} must be between {lo} and {hi}")
        return v
    offset = num("offset_mhz", -10, 10, "the repeater offset (MHz, like -5 or 0.6; 0 = simplex)")
    tone = num("tone_hz", 60, 260, "the repeater tone (Hz, like 100.0)")
    beats = body.get("beats") or []
    if not isinstance(beats, list) or not all(isinstance(b, str) and re.fullmatch(r"[\w-]{1,24}", b) for b in beats):
        raise ValueError("beats must be a list of net ids")
    out = {"name": name, "radio": rid, "freq_mhz": f, "mode": mode, "start": start, "duration_min": dur, "ncs": ncs,
           "repeat": rule, "exceptions": recur.normalize_exceptions(body.get("exceptions"), MODES) or None,
           "alert_min": alert, "emergency": True if body.get("emergency") else None,
           "offset_mhz": offset, "tone_hz": tone,             # CAT: set on the radio with the frequency (station/cat.py)
           "beats": sorted(set(beats)) or None}               # clashes settled: these nets lose to this one
    return {k: v for k, v in out.items() if v is not None}


def occurrences(net: dict, at: datetime, back_days=1, ahead_days=8):
    """(start, end) of each time the net runs from `back_days` before `at` to `ahead_days` after it."""
    for o in recur.occurrences(net, at - timedelta(days=back_days), at + timedelta(days=ahead_days)):
        yield o["start"], o["end"]


def item(n: dict, o: dict) -> dict:
    """One occurrence as the dashboard, the alerts and the history see it."""
    return {"id": n.get("id"), "name": n.get("name"), "radio": o.get("radio") or n.get("radio"), "date": o["date"],
            "key": recur.key(n.get("id"), o["date"]), "start": o["start"].isoformat(timespec="minutes"),
            "end": o["end"].isoformat(timespec="minutes"), "freq_mhz": o.get("freq_mhz"), "mode": o.get("mode"),
            "ncs": n.get("ncs"), "note": o.get("note"), "moved": o.get("moved", False),
            "emergency": bool(n.get("emergency")), "offset_mhz": n.get("offset_mhz"), "tone_hz": n.get("tone_hz")}


def may_be_on(n: dict, rid: str) -> bool:
    """`n` meets on radio `rid` at least sometimes: its own radio, or one date moved there."""
    return n.get("radio") == rid or any((e or {}).get("radio") == rid for e in (n.get("exceptions") or {}).values())


def on_radio(n: dict, o: dict) -> str | None:
    """The radio one occurrence meets on: a date moved to another radio, else the net's own."""
    return o.get("radio") or n.get("radio")


QUIET_MIN = 10          # an HF net on a CAT radio with nothing heard this long: try another net scheduled then
QUIET_MAX_MHZ = 54.0    # HF and 6 m (a repeater net is either up or it isn't)
UNHEARD_RUN = 3         # a net not heard this many times in a row is flagged "Not heard" (Remove / Keep it)


def listen_window(nets: list[dict], rid: str, at: datetime, lead=LEAD_MIN, grace=GRACE_MIN, skip=(), usable=None,
                  loses=None, dead=()):
    """(the net whose listening window [start - lead, end + grace) holds `at`, or None;
    the next net on this radio whose window opens after `at`, or None). Pure: the ticker and tests use it.
    `skip`: occurrence keys the operator chose to skip (not listened to). `usable(net)`: False = the radio can't hear
    it (station/fit.py) — not listened to or tuned for. `loses(net)`: it loses a settled clash now, and `dead`: keys
    of nets found quiet just now (owner 11-10: "if the radio can't hear anything for a current net and there is
    another net scheduled at the same time it should switch"): both are listened to only when nothing better is on."""
    cur, nxt, cands = None, None, []
    for n in nets:
        if not may_be_on(n, rid):
            continue
        ok = None                                      # usable(): once per net, only if it meets on this radio
        for o in recur.occurrences(n, at - timedelta(days=1), at + timedelta(days=LOOK_AHEAD_DAYS)):
            if on_radio(n, o) != rid:
                continue
            if ok is None:
                ok = usable is None or bool(usable(n | {"radio": rid}))
            if not ok:
                break
            it = item(n, o)
            if it["key"] in skip:
                continue
            s, e = o["start"], o["end"]
            if s - timedelta(minutes=lead) <= at < e + timedelta(minutes=grace):
                cands.append((it["key"] in dead, bool(loses and loses(n)), it))
            elif s - timedelta(minutes=lead) > at and (nxt is None or it["start"] < nxt["start"]) \
                    and not (loses and loses(n)):
                nxt = it
    for d, lo, it in cands:              # quiet ones last, then clash losers; back-to-back: the one starting now
        if cur is None or (d, lo) < cur[0] or ((d, lo) == cur[0] and it["start"] > cur[1]["start"]):
            cur = ((d, lo), it)
    return (cur[1] if cur else None), nxt


def occurrence_at(nets: list[dict], rid: str, at: datetime, before=BELONGS_MIN, after=BELONGS_MIN,
                  freq_mhz: float | None = None, ended=()) -> dict | None:
    """The occurrence on radio `rid` a transmission at `at` belongs to: from `before` minutes before it starts to
    `after` minutes after it ends. When two overlap (back-to-back nets on one radio), in this order: the one on the
    transmission's frequency, then the one whose scheduled time holds `at`, then the nearest start.
    (A tie on the nearest start alone once put a 20:45 simplex net's transmissions at 20:52 into the 21:00 net.)
    `ended`: occurrence keys that are over although their scheduled time is not (closed, or quiet for longer than
    the gap that ends a conversation): they lose to any other net that could take the transmission (the next net's
    early check-ins), but a lone net keeps its own after a long quiet spell. None = not a scheduled net."""
    best = None
    for n in nets:
        if not may_be_on(n, rid):
            continue
        for o in recur.occurrences(n, at - timedelta(days=1), at + timedelta(days=1)):
            if on_radio(n, o) != rid:
                continue
            if o["start"] - timedelta(minutes=before) <= at < o["end"] + timedelta(minutes=after):
                over = recur.key(n.get("id"), o["date"]) in ended     # ranks last: wins only when nothing else can
                f = o.get("freq_mhz") or n.get("freq_mhz")
                off_freq = bool(freq_mhz and f and abs(float(f) - float(freq_mhz)) > 0.0005)
                outside = not (o["start"] <= at < o["end"])
                rank = (over, off_freq, outside, abs((at - o["start"]).total_seconds()))
                if best is None or rank < best[0]:
                    best = (rank, item(n, o))
    return best[1] if best else None


def overrun(nets: list[dict], rid: str, at: datetime, last_heard: dict, closed=(), quiet=OVERRUN_QUIET_MIN,
            most=OVERRUN_MAX_MIN, lead=LEAD_MIN) -> dict | None:
    """The occurrence on radio `rid` still going past its scheduled end at `at` (owner, 2026-11-03: "let the net run
    until it is done or until just before the next net"), or None. Going = its end has passed, it was heard
    (`last_heard`: occurrence key -> datetime of its latest transmission) within `quiet` minutes, it was not closed
    (`closed`), it is at most `most` minutes past its end, and no other net on this radio is due (`lead` minutes
    before its start). Pure: the ticker, the net tagging and the tests use it."""
    best = None
    due = False
    for n in nets:
        if not may_be_on(n, rid):
            continue
        for o in recur.occurrences(n, at - timedelta(days=1), at + timedelta(days=1)):
            if on_radio(n, o) != rid:
                continue
            k = recur.key(n.get("id"), o["date"])
            if o["start"] - timedelta(minutes=lead) <= at < o["end"]:
                due = True                                      # another net is on (or about to be): it wins
            elif o["end"] <= at <= o["end"] + timedelta(minutes=most) and k not in closed:
                h = last_heard.get(k)
                if h and at - h <= timedelta(minutes=quiet) and h >= o["end"] - timedelta(minutes=quiet):
                    if best is None or o["end"] > best[0]:
                        best = (o["end"], item(n, o))
    if due or not best:
        return None
    return best[1] | {"overrun": True}


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
        self._quiet: dict[str, dict] = {}         # radio -> {"key", "since": listening to it since, "dead": quiet keys}
        self._heard = self._load_heard()          # net id -> [{"date", "heard", "min"} | {"kept": date}] (newest last)
        self.st.cfg["schedule"] = self._load()
        self._apply_saved()
        from .alerts import Alerts
        self.alerts = Alerts(station, self)

    # ---------- quiet nets (owner 2026-11-10) ----------
    def _heard_since(self, rid: str, since: datetime) -> bool:
        s = since.isoformat(timespec="seconds")
        return any(t.get("radio") == rid and str(t.get("start") or "") >= s for t in list(self.st.tx))

    def _quiet_check(self, rid, r, cur, nets, at, skip):
        """An HF net on a CAT radio with nothing heard for QUIET_MIN while another net is scheduled now: mark it quiet
        and return the other one (the radio retunes to it). With every candidate quiet it stays where it is."""
        q = self._quiet[rid]
        cat = getattr(self.st, "cat", None)
        if not cur or cat is None or not cat.has(rid) or r.get("paused"):
            return None
        if not cur.get("freq_mhz") or float(cur["freq_mhz"]) > QUIET_MAX_MHZ:
            return None
        if q["key"] != cur["key"] or not q["since"] or at - q["since"] < timedelta(minutes=QUIET_MIN):
            return None
        if self._heard_since(rid, q["since"]):
            return None
        alt, _ = listen_window(nets, rid, at, skip=skip, usable=self.usable(rid, at), loses=self.loses(at),
                               dead=q["dead"] | {cur["key"]})
        if not alt or alt["key"] == cur["key"] or alt["key"] in q["dead"]:
            return None
        q["dead"].add(cur["key"])
        print(f"{at:%H:%M:%S} {rid}: nothing heard on {cur['name']} for {QUIET_MIN} min -> {alt['name']}", flush=True)
        r.setdefault("listen", {})["switched"] = {"from": cur["name"], "to": alt["name"], "at": at.isoformat(timespec="minutes")}
        return alt

    @property
    def heard_path(self):
        return self.st.data / "net_heard.json"

    def _load_heard(self) -> dict:
        try:
            return json.loads(self.heard_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return {}

    def _save_heard(self):
        try:
            self.heard_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.heard_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._heard, indent=1), encoding="utf-8")
            os.replace(tmp, self.heard_path)
        except OSError as e:
            print(f"net_heard: {e}", flush=True)

    def _heard_end(self, rid, key, at):
        """The radio stops listening to `key`: note whether anything was heard (counted after 10 minutes of listening,
        or when something was heard at all)."""
        q = self._quiet.get(rid) or {}
        if not key or q.get("key") != key or not q.get("since"):
            return
        mins = int((at - q["since"]).total_seconds() // 60)
        heard = self._heard_since(rid, q["since"])
        if not heard and mins < QUIET_MIN:
            return
        nid, _, date = key.partition("@")
        rows = [x for x in self._heard.get(nid, []) if x.get("date") != date or "kept" in x]
        rows.append({"date": date, "heard": heard, "min": mins})
        self._heard[nid] = rows[-12:]
        self._save_heard()
        if not heard:
            self.publish()

    def heard_of(self, nid: str) -> dict:
        """{"run": times in a row not heard (since the last Keep it), "last": the latest few}"""
        rows, run = self._heard.get(nid, []), 0
        for x in reversed(rows):
            if "kept" in x or x.get("heard"):
                break
            run += 1
        return {"run": run, "flag": run >= UNHEARD_RUN, "last": [x for x in rows if "kept" not in x][-5:]}

    def keep(self, nid: str) -> dict:
        """The operator keeps a net flagged "Not heard": the count starts again."""
        self._find(nid)
        self._heard.setdefault(nid, []).append({"kept": datetime.now().isoformat(timespec="minutes")})
        self._save_heard()
        self.publish()
        return self.list()

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
            if isinstance(n, dict) and n.get("radio") and n["radio"] not in self.st.radios:
                print(f"schedule: {n.get('name')!r} was on radio {n['radio']!r}, which is gone: no radio now", flush=True)
                n = n | {"radio": None}
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
        nets = self.st.cfg.get("schedule") or []
        try:
            st = self.statuses(at)
        except Exception as e:  # noqa: BLE001 - the list still shows without the checks
            print(f"schedule status: {type(e).__name__}: {e}", flush=True)
            st = {}
        day0 = at.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            skipped = set(self.alerts.skipped())
        except Exception:  # noqa: BLE001
            skipped = set()
        out = []
        for n in nets:
            nxt = [o for o in recur.upcoming(n, at, 2) if o["start"] > at][:1]      # the next start (not the one on now)
            week = []                       # each meeting today and the next 7 days, on the radio it meets on (columns)
            for o in recur.occurrences(n, day0, day0 + timedelta(days=8)):
                rid = on_radio(n, o)
                week.append({"date": o["date"], "start": o["start"].isoformat(timespec="minutes"),
                             "end": o["end"].isoformat(timespec="minutes"), "radio": rid, "moved_radio": bool(o.get("radio")),
                             "key": recur.key(n["id"], o["date"]), "skipped": recur.key(n["id"], o["date"]) in skipped,
                             "loses": bool(rid) and fit.loses_at(n | {"radio": rid}, nets, o["start"] + timedelta(minutes=1))})
            out.append(n | {"summary": recur.summary(n), "status": st.get(n["id"]), "week": week,
                            "heard": self.heard_of(n["id"]),
                            "next": item(n, nxt[0]) if nxt else None})
        return out

    def statuses(self, at=None) -> dict:
        cat = getattr(self.st, "cat", None)
        return fit.statuses(self.st.cfg.get("schedule") or [], self.st.radios, at or self.st.clock(),
                            has_cat=lambda rid: bool(cat is not None and cat.has(rid)))

    def usable(self, rid: str, at: datetime):
        """For listen_window: the radio can hear the net and it does not lose a settled clash now."""
        cat = getattr(self.st, "cat", None)
        radio, nets = self.st.radios.get(rid), self.st.cfg.get("schedule") or []
        has = bool(cat is not None and cat.has(rid))
        return lambda n: fit.fit(n, radio, has)["kind"] in ("ready", "tune")

    def loses(self, at: datetime):
        nets = self.st.cfg.get("schedule") or []
        return lambda n: fit.loses_at(n, nets, at)

    def unassign(self, radio_ids: set):
        """Radios removed in Radios…: their nets stay, with no radio."""
        with self.lock:
            nets = [dict(n) | ({"radio": None} if n.get("radio") in radio_ids else {}) for n in self.st.cfg["schedule"]]
            for n in nets:
                if n.get("radio") is None:
                    n.pop("radio", None)
            self._commit(nets)

    def publish(self):
        self.st.bus.publish("schedule", {"schedule": self.decorated(), "net": self.st.schedule_now()})

    def list(self) -> dict:
        return {"schedule": self.decorated(), "edited": self.path.exists(), "days": DAYS, "modes": list(MODES),
                "radios": [{"id": r["id"], "name": r["name"], "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode"),
                            "listen": r.get("listen"), "model": r.get("model"), "antenna": r.get("antenna"),
                            "role": r.get("role")} for r in self.st.radios.values()],
                "models": {k: v["label"] for k, v in fit.MODELS.items()},
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
            if "beats" not in body and nets[i].get("beats"):
                body = body | {"beats": nets[i]["beats"]}
            nets[i] = {"id": nid} | validate(body, self.st.radios)
            self._commit(nets)
        return self.list()

    def assign(self, nid: str, rid) -> dict:
        """Move a net to another radio (or to no radio: kept, not recorded)."""
        rid = str(rid or "").strip() or None
        if rid is not None and rid not in self.st.radios:
            raise ValueError(f"no radio called {rid!r}")
        with self.lock:
            nets, i = self._find(nid)
            nets[i] = {"id": nid} | validate(nets[i] | {"radio": rid}, self.st.radios)
            self._commit(nets)
        print(f"{datetime.now():%H:%M:%S} net {nets[i]['name']!r} -> radio {rid}", flush=True)
        return self.list()

    def settle(self, nid: str, other: str, undo=False) -> dict:
        """A clash on one radio: `nid` is recorded when it meets `other` (`other` is not listened to then)."""
        with self.lock:
            nets, i = self._find(nid)
            j = next((k for k, n in enumerate(nets) if n["id"] == other), None)
            if j is None:
                raise NoSuchNet(other)
            mine = set(nets[i].get("beats") or [])
            mine = mine - {other} if undo else mine | {other}
            nets[i] = {"id": nid} | validate(nets[i] | {"beats": sorted(mine)}, self.st.radios)
            if not undo:
                nets[j] = {"id": other} | validate(nets[j] | {"beats": [b for b in nets[j].get("beats") or []
                                                                       if b != nid]}, self.st.radios)
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
                    only_radio = "radio" in body and not any(body.get(k) not in (None, "") for k in
                                                             ("start", "duration_min", "freq_mhz", "mode", "to_date"))
                    if only_radio:                                   # a drag: keep that date's other changes
                        ex[occ] = {k: v for k, v in (ex.get(occ) or {}).items() if k != "cancel"} | \
                                  {"radio": body.get("radio") or n.get("radio")}
                    else:
                        ex[occ] = {k: body[k] for k in ("start", "duration_min", "freq_mhz", "mode", "note", "radio")
                                   if body.get(k) not in (None, "")}
                    if ex[occ].get("radio") == n.get("radio"):
                        ex[occ].pop("radio")                         # back on its own radio
                    elif ex[occ].get("radio") and ex[occ]["radio"] not in self.st.radios:
                        raise ValueError(f"no radio called {ex[occ]['radio']!r}")
                    if body.get("to_date"):                          # moved to another day
                        ex[occ]["date"] = body["to_date"]
            n["exceptions"] = recur.normalize_exceptions(ex, MODES)
            if not n["exceptions"]:
                n.pop("exceptions")
            nets[i] = {"id": nid} | validate(n, self.st.radios)
            self._commit(nets)
        return self.list()

    def preview(self, body: dict) -> dict:
        """The editor's live preview of a net being typed (nothing saved). A rule still being typed is not a failed
        request: its problem comes back as {"error"} so the editor can show it in place."""
        try:
            v = validate(body, self.st.radios)
        except ValueError as e:
            return {"error": str(e)}
        return preview(v, self.st.clock())

    def post(self, path: str, body: dict) -> dict:
        """POST /api/schedule (add), /api/schedule/preview, /api/schedule/<id> (replace), /api/schedule/<id>/delete,
        /api/schedule/<id>/skip|move|restore ({"date": the occurrence's own date; move: to_date, start, freq_mhz,
        mode, duration_min, note})."""
        m = re.fullmatch(r"/api/schedule(?:/([\w-]+)(?:/(delete|skip|move|restore|radio|settle|keep))?)?", path)
        if not m:
            raise NoSuchNet(path)
        if not m.group(1):
            return self.add(body)
        if m.group(1) == "preview" and not m.group(2):
            return self.preview(body)
        if m.group(2) == "delete":
            return self.delete(m.group(1))
        if m.group(2) == "keep":                  # a net flagged "Not heard": keep it, count again
            return self.keep(m.group(1))
        if m.group(2) == "radio":                 # {"radio": id | null}: the chip on the net
            return self.assign(m.group(1), body.get("radio"))
        if m.group(2) == "settle":                # {"over": other net id}: this one is recorded when they meet
            return self.settle(m.group(1), str(body.get("over") or ""), bool(body.get("undo")))
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
        # the header's Today strip: re-send when a net starts, ends, closes or the next one changes (checked each minute)
        if at.second == 0 or not hasattr(self, "_today_sig"):
            try:
                now = self.st.schedule_now(at)
                sig = [(x["key"], x["state"], x.get("skipped")) for x in now["today"]]
                if sig != getattr(self, "_today_sig", None):
                    self._today_sig = sig
                    self.st.bus.publish("today", {"net": now})
            except Exception as e:  # noqa: BLE001 — the strip never stops listening
                print(f"today: {type(e).__name__}: {e}", flush=True)
        closer = getattr(self.st, "closer", None)
        held = lambda rid, key: bool(closer and closer.holds(rid, key))   # a net closed early stays paused
        with self.lock:
            for rid, r in list(self.st.radios.items()):
                L = r.setdefault("listen", {"scheduled": False, "tune": False})
                q = self._quiet.setdefault(rid, {"key": None, "since": None, "dead": set()})
                cur, nxt = listen_window(nets, rid, at, skip=skip, usable=self.usable(rid, at), loses=self.loses(at),
                                         dead=q["dead"])
                cur = self._quiet_check(rid, r, cur, nets, at, skip) or cur
                if cur is None:
                    try:
                        cur = self.st.overrun(rid, at)               # still going past its end: keep listening
                    except Exception as e:  # noqa: BLE001
                        print(f"overrun: {type(e).__name__}: {e}", flush=True)
                        cur = None
                L["now"] = cur["name"] if cur else None
                L["next"] = {"name": nxt["name"], "start": nxt["start"]} if nxt else None
                want = cur["key"] if cur else ""                       # which net window (edge = a change)
                first = rid not in self._want
                if self._want.get(rid) == want:
                    continue
                self._heard_end(rid, self._want.get(rid), at)          # the net it leaves: heard or not
                q["key"], q["since"] = (want or None), at
                if not want:
                    q["dead"] = set()
                self._want[rid] = want
                cat = getattr(self.st, "cat", None)
                if cat is not None and cat.has(rid):
                    # CAT (station/cat.py): tune the radio to the net at its start, put it back at the end
                    cat.on_net(rid, cur if want else None)
                if not L.get("scheduled"):
                    # A scheduled net is always recorded (owner, 2026-11-03): a radio paused by hand is resumed
                    # LEAD_MIN before its net and put back to paused GRACE_MIN after it, unless the operator
                    # pressed Pause/Resume in between (set_paused(by="operator") clears resumed_for).
                    if want:
                        if r.get("paused") and not (first and r.get("paused_by") == "operator") \
                                and not held(rid, want):
                            L["resumed_for"] = want
                            self.st.set_paused(rid, False, by="schedule")
                            print(f"{at:%H:%M:%S} {rid} resumed for {cur['name']} (it was paused)", flush=True)
                    elif L.pop("resumed_for", None) and not r.get("paused"):
                        self.st.set_paused(rid, True, by="schedule")
                        print(f"{at:%H:%M:%S} {rid} paused again after its net (as before)", flush=True)
                    continue
                if want:
                    # a restart inside a net keeps a pause the operator chose; a net's start always resumes
                    if r.get("paused") and not (first and r.get("paused_by") == "operator") and not held(rid, want):
                        self.st.set_paused(rid, False, by="schedule")
                    f, mode = cur.get("freq_mhz"), cur.get("mode")
                    if L.get("tune") and f and (abs(f - (r.get("freq_mhz") or 0)) > 0.0005
                                                or (mode and mode != r.get("mode"))):
                        self.st.set_radio(rid, f, mode)
                        print(f"{datetime.now():%H:%M:%S} {rid} logged as {f} MHz {mode or ''} for {cur['name']}",
                              flush=True)
                elif not r.get("paused") and not (first and r.get("resumed_by") == "operator"):
                    # a restart outside a net keeps a Resume the operator chose (two restarts on 11-06 paused a radio
                    # the operator had just resumed to compare it with another station); the next net's end pauses it
                    self.st.set_paused(rid, True, by="schedule")
