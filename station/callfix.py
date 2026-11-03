"""The station's side of near-miss callsign correction (hamkit/callfix.py does the judging): what this station
knows when it reads one transmission. Live text sees the net so far; the accurate pass, which runs after the
net, sees the whole net (a call heard later corrects an earlier garble: 'WA7BXW' at 19:06 -> WA7CXW at 19:07)."""
from collections import Counter
from datetime import datetime, timedelta

from hamkit.callfix import DISTRICT, CallFixer, Context

GAP = timedelta(minutes=15)          # the same session rule as ics.sessions and the dashboard


def _calls(t: dict) -> list[str]:
    return (t.get("accurate") or t).get("callsigns", []) or []


def _session(rows: list[dict], i: int, whole: bool) -> list[dict]:
    """The transmissions of row i's net (same radio + frequency, no gap over 15 min), row i excluded."""
    me = rows[i]
    out = []
    def walk(rng):
        last = datetime.fromisoformat(me["start"])
        for j in rng:
            t = rows[j]
            at = datetime.fromisoformat(t["start"])
            f1, f2 = t.get("freq_mhz"), me.get("freq_mhz")
            if abs(at - last) > GAP or (f1 and f2 and f1 != f2):     # no frequency yet = the same net
                break
            out.append(t); last = at
    walk(range(i - 1, -1, -1))
    if whole:
        walk(range(i + 1, len(rows)))
    return out


def context(station, tx: dict, whole_net: bool = False) -> Context:
    with station.lock:
        rows = sorted((t for t in station.tx if t.get("radio") == tx.get("radio")
                       and (t.get("status") == "final" or t is tx)), key=lambda t: t["start"])
        i = next((k for k, t in enumerate(rows) if t is tx or t.get("id") == tx.get("id")), None)
        net = Counter(c for t in (_session(rows, i, whole_net) if i is not None else []) for c in _calls(t))
        today = {c for t in station.tx if t.get("status") == "final" and t.get("id") != tx.get("id")
                 and t.get("start", "")[:10] == tx.get("start", "")[:10] for c in _calls(t)}
    fixer = fixer_for(station)
    home = home_state(station)
    digits = Counter(ch for c in net for ch in c if ch.isdigit())
    districts = {DISTRICT.get(home)} | {d for d, _ in digits.most_common(1)}
    return Context(me=station.me, roster=station.roster, net=net, today=today, home_state=home,
                   districts=districts - {None})


def fixer_for(station) -> CallFixer:
    f = getattr(station, "_callfix", None)
    if f is None:
        cb = station.callbook
        f = station._callfix = CallFixer(getattr(cb, "con", None), getattr(cb, "lock", None),
                                         cb.validate if cb else None)
    return f


def home_state(station) -> str | None:
    """The operator's FCC state, or `region_state:` in station.yaml (for a call licensed elsewhere)."""
    if station.cfg.get("region_state"):
        return str(station.cfg["region_state"]).upper()
    key = ("_home", station.me)
    cache = station.__dict__.setdefault("_callfix_home", {})
    if key not in cache:
        cache[key] = fixer_for(station).home(station.me)
    return cache[key]


def apply(station, tx: dict | None, text: str, calls: list[str], whole_net: bool = False):
    """-> (text, calls, extra) where extra = {"corrections": [...], "maybe": [...]} (keys only when non-empty).
    Never raises: a failure here leaves the transmission as normalize() read it."""
    if tx is None or not station.cfg.get("callfix", True):
        return text, calls, {}
    try:
        text2, calls2, corr, maybe = fixer_for(station).fix(text, calls, context(station, tx, whole_net))
    except Exception as e:  # noqa: BLE001 — a correction is extra; never lose a transmission over it
        print(f"{datetime.now():%H:%M:%S} callfix FAILED on {tx.get('id')}: {type(e).__name__}: {e}", flush=True)
        return text, calls, {}
    extra = {k: v for k, v in (("corrections", corr), ("maybe", maybe)) if v}
    return text2, calls2, extra
