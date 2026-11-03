"""When a net meets: Outlook-style repetition, as a small readable JSON rule, and pure functions over it.

A net in the schedule (data/schedule.json) carries its rule in `repeat` and one-off changes in `exceptions`:

    {"name": "Tuesday Net", "radio": "ftm", "start": "19:00", "duration_min": 60, "freq_mhz": 146.94, "mode": "FM",
     "repeat": {"freq": "monthly", "nth": [1, 3], "weekdays": ["Sun"], "starts": "2026-01-04"},
     "exceptions": {"2026-12-20": {"cancel": true, "note": "Holiday"},
                    "2027-01-03": {"date": "2027-01-04", "start": "19:30", "freq_mhz": 147.0}}}

`repeat` (every key but `freq` is optional):
    freq       once | daily | weekly | monthly | yearly
    interval   every N days / weeks / months / years (default 1; more than 1 needs `starts`, the anchor)
    weekdays   weekly: the days it meets. monthly/yearly with `nth`: the weekday(s)
    nth        monthly/yearly: which of the month's weekdays, 1-5 or -1 for the last ("1st and 3rd Sunday": [1, 3])
    monthday   monthly/yearly: day 1-31, or -1 for the last day. A day the month does not have means its last day
               (Outlook does the same: "day 31" meets on 30 April and 28/29 February)
    month      yearly: 1-12
    starts     first date it can meet (YYYY-MM-DD); `once` meets on this date only
    until      last date it can meet, or
    count      it ends after this many occurrences (counted from `starts`)

`exceptions` are keyed by the date an occurrence WOULD have been: {"cancel": true} drops it; `date`, `start`,
`duration_min`, `freq_mhz`, `mode` move or change it; `note` is shown with it.

Times are the PC's local wall-clock time, naive on purpose: a 19:00 net is at 19:00 on both sides of a daylight
saving change. The old weekly form (`days: [Tue, Thu]`) is read as {"freq": "weekly", "weekdays": [...]}.
"""
import calendar
import json
import re
from datetime import date, datetime, time, timedelta
from functools import lru_cache

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
FREQS = ("once", "daily", "weekly", "monthly", "yearly")
ORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", -1: "last"}
SCAN_YEARS = 60          # how far `count` is counted from `starts` (a daily net that ends after 999 times: 3 years)


# ---------- validation ----------
def _date(v, what) -> str | None:
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{what} must be a date like 2026-11-03") from None


def _int(v, what, lo, hi) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a whole number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be {lo} to {hi}")
    return n


def weekdays(v, what="days") -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\s]+", v.strip())
    if not isinstance(v, (list, tuple)) or not [d for d in v if str(d).strip()]:
        raise ValueError("choose at least one day")
    out = set()
    for d in v:
        d = str(d).strip()[:3].title()
        if d not in DAYS:
            raise ValueError(f"{what} must be Mon, Tue, Wed, Thu, Fri, Sat or Sun (got {d or 'nothing'})")
        out.add(d)
    return [d for d in DAYS if d in out]


def normalize(rule, legacy_days=None) -> dict:
    """A repeat rule from the dashboard or station.yaml, checked and in its canonical form. Raises ValueError."""
    if rule in (None, "", {}):
        return {"freq": "weekly", "weekdays": weekdays(legacy_days if legacy_days not in (None, "") else DAYS)}
    if not isinstance(rule, dict):
        raise ValueError("repeat must be an object like {\"freq\": \"weekly\", \"weekdays\": [\"Tue\"]}")
    f = str(rule.get("freq") or "").lower().strip()
    f = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly", "one": "once"}.get(f, f)
    if f not in FREQS:
        raise ValueError("repeat must be once, daily, weekly, monthly or yearly")
    out = {"freq": f}
    interval = _int(rule.get("interval", 1) or 1, "repeat every", 1, 99)
    starts, until = _date(rule.get("starts"), "starts on"), _date(rule.get("until"), "ends on")
    if f == "once":
        if not starts:
            raise ValueError("choose the date of the net")
        return {"freq": "once", "starts": starts}
    if interval > 1:
        out["interval"] = interval
        if not starts:
            raise ValueError("every 2 or more needs a start date (the first time it meets)")
    if f == "weekly":
        out["weekdays"] = weekdays(rule.get("weekdays") if rule.get("weekdays") not in (None, "") else legacy_days)
    elif f in ("monthly", "yearly"):
        if f == "yearly":
            m = rule.get("month") or (date.fromisoformat(starts).month if starts else None)
            if not m:
                raise ValueError("choose the month")
            out["month"] = _int(m, "month", 1, 12)
        if rule.get("nth") not in (None, "", []):
            nth = rule["nth"] if isinstance(rule["nth"], (list, tuple)) else re.split(r"[,\s]+", str(rule["nth"]))
            ns = set()
            for n in nth:
                n = {"last": -1, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}.get(str(n).strip().lower(), n)
                n = _int(n, "which week", -1, 5)
                if n == 0:
                    raise ValueError("which week must be 1st to 5th or last")
                ns.add(n)
            out["nth"] = sorted(ns, key=lambda n: n if n > 0 else 9)
            out["weekdays"] = weekdays(rule.get("weekdays"), "weekday")
        else:
            md = rule.get("monthday")
            if md in (None, ""):
                if not starts:
                    raise ValueError("choose the day of the month")
                md = date.fromisoformat(starts).day
            md = -1 if str(md).strip().lower() == "last" else _int(md, "day of the month", -1, 31)
            if md == 0:
                raise ValueError("day of the month must be 1 to 31 or last")
            out["monthday"] = md
    if starts:
        out["starts"] = starts
    if until and rule.get("count") not in (None, ""):
        raise ValueError("end on a date or after a number of times, not both")
    if until:
        if starts and until < starts:
            raise ValueError("the end date is before the start date")
        out["until"] = until
    elif rule.get("count") not in (None, ""):
        if not starts:
            raise ValueError("ending after a number of times needs a start date")
        out["count"] = _int(rule["count"], "number of times", 1, 999)
    return out


def normalize_exceptions(ex, modes=("FM", "USB", "LSB", "AM", "CW", "DIG")) -> dict:
    if not ex:
        return {}
    if not isinstance(ex, dict):
        raise ValueError("exceptions must be an object keyed by date")
    out = {}
    for k, v in ex.items():
        k = _date(k, "the occurrence")
        if not isinstance(v, dict):
            raise ValueError("an exception must be an object")
        e = {}
        if v.get("cancel"):
            e["cancel"] = True
        else:
            if v.get("date") not in (None, ""):
                e["date"] = _date(v["date"], "moved to")
            if v.get("start") not in (None, ""):
                e["start"] = hhmm(v["start"])
            if v.get("duration_min") not in (None, ""):
                e["duration_min"] = _int(v["duration_min"], "duration", 1, 1440)
            if v.get("freq_mhz") not in (None, ""):
                try:
                    e["freq_mhz"] = round(float(str(v["freq_mhz"]).replace(",", ".")), 4)
                except ValueError:
                    raise ValueError("frequency must be a number in MHz") from None
            if v.get("mode"):
                m = str(v["mode"]).upper()
                if m not in modes:
                    raise ValueError(f"mode must be one of {', '.join(modes)}")
                e["mode"] = m
        if v.get("note"):
            e["note"] = str(v["note"]).strip()[:120]
        if e:
            out[k] = e
    return dict(sorted(out.items()))


def hhmm(v) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(v or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("start must be a time like 20:00 (24-hour clock)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


# ---------- does it meet on this date? ----------
def _last_day(y, m) -> int:
    return calendar.monthrange(y, m)[1]


def _day_rule(rule, d: date) -> bool:
    """monthly/yearly: the day-of-month or nth-weekday part."""
    if "nth" in rule:
        if DAYS[d.weekday()] not in rule["weekdays"]:
            return False
        k = (d.day - 1) // 7 + 1
        return k in rule["nth"] or (-1 in rule["nth"] and d.day + 7 > _last_day(d.year, d.month))
    md, last = rule.get("monthday", 1), _last_day(d.year, d.month)
    return d.day == last if md == -1 or md >= last else d.day == md


def matches(rule: dict, d: date) -> bool:
    """True when the rule meets on `d` (ignoring `count` and exceptions)."""
    st = date.fromisoformat(rule["starts"]) if rule.get("starts") else None
    if st and d < st or rule.get("until") and d > date.fromisoformat(rule["until"]):
        return False
    f, n = rule["freq"], rule.get("interval", 1)
    if f == "once":
        return d == st
    if f == "daily":
        return (d - (st or date(1970, 1, 1))).days % n == 0
    if f == "weekly":
        if DAYS[d.weekday()] not in rule["weekdays"]:
            return False
        a = st or date(1970, 1, 5)
        return ((d - timedelta(d.weekday())) - (a - timedelta(a.weekday()))).days // 7 % n == 0
    if f == "monthly":
        a = st or date(1970, 1, 1)
        return ((d.year * 12 + d.month) - (a.year * 12 + a.month)) % n == 0 and _day_rule(rule, d)
    if f == "yearly":
        a = st or date(1970, 1, 1)
        return d.month == rule["month"] and (d.year - a.year) % n == 0 and _day_rule(rule, d)
    return False


@lru_cache(maxsize=256)
def _counted(key: str) -> tuple[str, ...]:
    """The first `count` dates of a rule that ends after a number of times."""
    rule = json.loads(key)
    out, d = [], date.fromisoformat(rule["starts"])
    stop = d + timedelta(days=366 * SCAN_YEARS)
    while d < stop and len(out) < rule["count"]:
        if matches(rule, d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return tuple(out)


@lru_cache(maxsize=2048)
def _dates(key: str, a: date, b: date) -> tuple[date, ...]:
    rule = json.loads(key)
    if rule.get("count"):
        return tuple(d for d in map(date.fromisoformat, _counted(key)) if a <= d <= b)
    out, d = [], a
    while d <= b:
        if matches(rule, d):
            out.append(d)
        d += timedelta(days=1)
    return tuple(out)


def dates(rule: dict, a: date, b: date) -> list[date]:
    """Every date from a to b (inclusive) the rule meets on, honouring starts / until / count."""
    return list(_dates(json.dumps(rule, sort_keys=True), a, b))


# ---------- occurrences of a net ----------
def rule_of(net: dict) -> dict:
    return net.get("repeat") or normalize(None, net.get("days"))


def _at(d: date, hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime.combine(d, time(h, m))


MOVE_REACH = 40          # an occurrence can be moved this many days either way and still be found


def occurrences(net: dict, frm: datetime, to: datetime, cancelled=False) -> list[dict]:
    """Each time `net` meets that overlaps [frm, to), oldest first, with its exceptions applied:
    {"date": the date it would have been (its key), "start", "end" (datetimes), "freq_mhz", "mode", "moved",
     "note", "cancelled" (only with cancelled=True)}."""
    rule, ex = rule_of(net), net.get("exceptions") or {}
    dur = int(net.get("duration_min", 60))
    out = []
    reach = timedelta(days=MOVE_REACH if ex else 1)
    for d in dates(rule, (frm - reach).date(), (to + reach).date()):
        e = ex.get(d.isoformat()) or {}
        if e.get("cancel") and not cancelled:
            continue
        nd = date.fromisoformat(e["date"]) if e.get("date") else d
        s = _at(nd, e.get("start") or net.get("start", "00:00"))
        en = s + timedelta(minutes=int(e.get("duration_min") or dur))
        if not (en > frm and s < to):
            continue
        o = {"date": d.isoformat(), "start": s, "end": en, "freq_mhz": e.get("freq_mhz", net.get("freq_mhz")),
             "mode": e.get("mode", net.get("mode")), "moved": bool({"date", "start", "duration_min", "freq_mhz",
                                                                   "mode"} & set(e)), "note": e.get("note")}
        if e.get("cancel"):
            o["cancelled"] = True
        out.append(o)
    return sorted(out, key=lambda o: o["start"])


def upcoming(net: dict, at: datetime, n: int = 5, cancelled=False, horizon_days=800) -> list[dict]:
    """The next `n` times the net meets that have not ended yet."""
    out, frm, step = [], at, 62
    while len(out) < n and frm < at + timedelta(days=horizon_days):
        to = frm + timedelta(days=step)
        for o in occurrences(net, frm, to, cancelled):
            if o["end"] > at and o["start"] >= frm - timedelta(days=1) and o not in out:
                out.append(o)
        frm = to
    seen, uniq = set(), []
    for o in sorted(out, key=lambda o: o["start"]):
        if o["date"] not in seen:
            seen.add(o["date"]); uniq.append(o)
    return uniq[:n]


def key(net_id: str, occ_date: str) -> str:
    """One occurrence of one net: its history instance key."""
    return f"{net_id}@{occ_date}"


# ---------- plain words ----------
def _join(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _dn(ds: list[str]) -> str:
    return _join([DAY_NAMES[DAYS.index(d)] for d in ds])


def _fmt_date(s: str) -> str:
    d = date.fromisoformat(s)
    return f"{d.day} {MONTHS[d.month - 1][:3]} {d.year}"


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe(rule: dict) -> str:
    """'Every 1st and 3rd Sunday', 'Every 2 weeks on Tuesday and Thursday', 'Every year on 27 June'."""
    f, n = rule["freq"], rule.get("interval", 1)
    if f == "once":
        return "Once, on " + DAY_NAMES[date.fromisoformat(rule["starts"]).weekday()] + " " + _fmt_date(rule["starts"])
    if f == "daily":
        return "Every day" if n == 1 else f"Every {n} days"
    if f == "weekly":
        ds = rule["weekdays"]
        what = "weekday" if ds == DAYS[:5] else "day" if ds == DAYS else None
        if n == 1:
            return f"Every {what}" if what else f"Every {_dn(ds)}"
        return f"Every {n} weeks on " + ("weekdays" if what == "weekday" else "every day" if what else _dn(ds))
    which = ""
    if "nth" in rule:
        which = _join([ORD[k] for k in rule["nth"]]) + " " + _join([DAY_NAMES[DAYS.index(d)] for d in rule["weekdays"]])
    if f == "monthly":
        if "nth" in rule:
            return f"Every {which}" + ("" if n == 1 else f" of every {n} months")
        md = rule.get("monthday", 1)
        day = "the last day" if md == -1 else f"day {md}"
        return f"On {day} of every month" if n == 1 else f"On {day} of every {n} months"
    mon = MONTHS[rule["month"] - 1]
    every = "Every year" if n == 1 else f"Every {n} years"
    if "nth" in rule:
        return f"{every} on the {which} of {mon}"
    md = rule.get("monthday", 1)
    return f"{every} on the last day of {mon}" if md == -1 else f"{every} on {md} {mon}"


def summary(net: dict) -> str:
    """'Every 1st and 3rd Sunday at 19:00, 146.940 MHz FM, 60 min, until 29 Dec 2026'."""
    rule = rule_of(net)
    s = describe(rule) + f" at {net.get('start', '00:00')}"
    if net.get("freq_mhz"):
        s += f", {float(net['freq_mhz']):.3f} MHz" + (f" {net['mode']}" if net.get("mode") else "")
    elif net.get("mode"):
        s += f", {net['mode']}"
    s += f", {int(net.get('duration_min', 60))} min"
    if rule["freq"] != "once":
        if rule.get("starts") and rule.get("starts") > date.today().isoformat():
            s += f", from {_fmt_date(rule['starts'])}"
        if rule.get("until"):
            s += f", until {_fmt_date(rule['until'])}"
        elif rule.get("count"):
            s += f", {rule['count']} time{'s' if rule['count'] != 1 else ''}"
    return s


def ended(net: dict, at: datetime) -> bool:
    """True when the rule can never meet again after `at`."""
    return not upcoming(net, at, 1)
