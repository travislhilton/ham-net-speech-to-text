"""Find nets (Nets -> Find nets): nets near the station found by an xAI web search, ticked and added to the schedule.

Needs the xAI key (Settings -> Station agent). Which bands are searched follows the radios on this PC (station/fit.py):
a 2 m / 70 cm radio -> VHF/UHF only, an HF radio -> HF only, both (or an IC-705, which does both with the right
antenna) -> both. A Winlink-only radio counts for nothing.

Where the station is, in this order (owner 2026-11-13): the grid square in Settings; else the position this PC's
browser reports (or station_lat/station_lon in station.yaml); else the address the operator types (its ZIP's centre
when the ZIP table is installed). Only a rounded position (0.1 degree, about 10 km) and the 4-character grid go to
xAI, never a street address: an address is reduced to its ZIP or "town, state".

Search results are AI-found and may be stale: a net is offered only with a frequency, a start time, its days and a
source link; each says whether the search actually opened that source. Results are kept a week (data/netfind.json),
so reopening the list costs nothing; "Search again" asks afresh (one search: roughly 10-60 s and about $0.10-0.30).
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import fit, recur
from . import schedule as sched

DEFAULT_KM = 50            # VHF/UHF: how far to look (owner: settable, 50 km by default)
DEFAULT_HF_KM = 800        # HF: regional nets (80 / 40 m reach this far at night with a simple antenna)
KEEP_S = 7 * 86400
MODEL = "grok-4.20-0309-non-reasoning"
TIMEOUT_S = 240
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MODES = fit.ALL_MODES

# ---------------------------------------------------------------- HF: what the operator wants, and can reach -------
# HF reach is station/hfprop.py: the antenna's gain at each path's takeoff angle, the MUF from today's solar flux, etc.
from . import hfprop  # noqa: E402

INTERESTS = {        # HF: what kinds of nets to look for (Find nets page; owner 11-14 "add interesting criteria")
    "emergency": ("Emergency / ARES / RACES", "ARES, RACES, state and regional emergency nets, EOC nets, SATERN, the "
                  "Hurricane Watch Net and other disaster-response nets"),
    "traffic": ("Traffic (NTS)", "National Traffic System nets: section, region and area phone nets"),
    "social": ("Ragchew / social", "ragchew, social, morning/coffee and regional get-together nets"),
    "technical": ("Technical / new hams", "technical, new-ham, Elmer / help, antenna and swap nets"),
    "special": ("Special interest", "special-interest nets: maritime mobile, RV and mobile, county hunters, QRP, "
                "veterans, faith-based and service-club nets"),
}
DEFAULT_INTERESTS = ["emergency", "traffic", "social", "technical"]


def _power_default(st) -> int:
    for r in st.radios.values():
        if r.get("role") != "winlink" and (r.get("model") or "") == "ic705":
            return 10
    return 100


def _rng(f) -> str:
    try:
        f = float(f)
    except (TypeError, ValueError):
        return ""
    return "HF" if f < 30 else "VHF" if f < 300 else "UHF"


# ---------------------------------------------------------------- what this station can hear --------------------
def reach(st) -> dict:
    """{"HF": bool, "VU": bool, "modes": [...], "radios": [names]}: what the radios here could record (any antenna)."""
    hf = vu = False
    modes: set[str] = set()
    names = []
    for r in st.radios.values():
        if r.get("role") == "winlink":
            continue
        m = fit.MODELS.get(r.get("model") or "any", fit.MODELS["any"])
        bands = m["bands"]
        hf = hf or any(a < 30 and b > 1.8 for a, b in bands)
        vu = vu or any(b > 144 and a < 450 for a, b in bands)
        modes |= set(m["modes"])
        names.append(r.get("name") or r["id"])
    return {"HF": hf, "VU": vu, "modes": sorted(modes), "radios": names}


# ---------------------------------------------------------------- where the station is ---------------------------
def _path(st):
    return st.data / "netfind.json"


def _load(st) -> dict:
    try:
        return json.loads(_path(st).read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(st, d: dict):
    p = _path(st)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(p)


def _grid4(lat: float, lon: float) -> str:
    lon += 180
    lat += 90
    return (chr(65 + int(lon // 20)) + chr(65 + int(lat // 10)) + str(int(lon % 20 // 2)) + str(int(lat % 10)))


def _area_of_address(text: str) -> tuple[str | None, str]:
    """(ZIP or None, what may be sent: ZIP or the address without its street part)."""
    t = " ".join(str(text or "").split())[:200]
    z = re.search(r"\b(\d{5})(?:-\d{4})?\b\s*$", t) or re.search(r"\b(\d{5})(?:-\d{4})?\b", t)
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if len(parts) > 1 and re.match(r"^\d", parts[0]):          # "12 Main St, Springfield, IL" -> "Springfield, IL"
        parts = parts[1:]
    area = ", ".join(parts)
    if z:
        area = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", area).strip(" ,") or z.group(1)
    return (z.group(1) if z else None), area


def _zip_where(z: str | None):
    if not z:
        return None
    try:
        from hamkit.zipgeo import ZipGeo
        return ZipGeo().where(z)
    except Exception:  # noqa: BLE001 - the ZIP table is optional (python -m hamkit.zipgeo update)
        return None


def location(st) -> dict | None:
    """{"lat", "lon", "source", "label", "send"} or None when nothing is known yet (the page asks for an address)."""
    saved = _load(st)
    grid = str(((st._saved().get("operator") or {}).get("grid")) or "").strip()
    if len(grid) >= 4:
        try:
            from hamkit.zipgeo import grid_to_latlon
            p = grid_to_latlon(grid[:6] if len(grid) >= 6 else grid[:4])
        except Exception:  # noqa: BLE001
            p = None
        if p:
            return {"lat": p[0], "lon": p[1], "source": "grid", "label": f"grid square {grid.upper()} (Settings)"}
    if st.cfg.get("station_lat") is not None and st.cfg.get("station_lon") is not None:
        lat, lon = float(st.cfg["station_lat"]), float(st.cfg["station_lon"])
        return {"lat": lat, "lon": lon, "source": "set", "label": "the station's position (station.yaml)"}
    g = saved.get("gps") or {}
    if g.get("lat") is not None and g.get("lon") is not None:
        return {"lat": float(g["lat"]), "lon": float(g["lon"]), "source": "gps",
                "label": f"this PC's location (about {g.get('acc_m') or '?'} m)" if g.get("acc_m") else "this PC's location"}
    a = saved.get("address") or ""
    if a:
        z, area = _area_of_address(a)
        p = _zip_where(z)
        if p:
            return {"lat": p[0], "lon": p[1], "source": "address", "label": f"ZIP {z} (your address)"}
        if area:
            return {"lat": None, "lon": None, "source": "address", "label": area, "area": area}
    return None


def _where_words(loc: dict) -> str:
    if loc.get("lat") is not None:
        lat, lon = round(loc["lat"], 1), round(loc["lon"], 1)                # about 10 km: enough for a 50 km search
        return f"latitude {lat}, longitude {lon} (Maidenhead grid {_grid4(loc['lat'], loc['lon'])})"
    return loc.get("area") or "?"


# ---------------------------------------------------------------- the search -------------------------------------
NET_KEYS = ("name", "freq_mhz", "mode", "offset_mhz", "tone_hz", "repeat", "weekdays", "nth", "start", "time_zone",
            "duration_min", "ncs", "area", "distance_km", "emergency", "source_url", "note")


_FIELDS = (
    "Answer ONLY with a JSON object, no prose: {\"nets\": [ ... up to %d ... ]} where each net is\n"
    "{\"name\": str, \"freq_mhz\": number (the listening frequency), \"mode\": one of FM USB LSB AM CW DIG (skip DMR,"
    " D-STAR, Fusion and other digital voice nets), \"offset_mhz\": number or null (repeater offset, e.g. 0.6 or -5),"
    " \"tone_hz\": number or null (CTCSS), \"repeat\": \"weekly\" | \"monthly\" | \"daily\", \"weekdays\": [\"Mon\"..\"Sun\"],"
    " \"nth\": [1-5 or -1] (monthly only, e.g. 2nd Tuesday = [2]), \"start\": \"HH:MM\" 24-hour,"
    " \"time_zone\": \"local\" | \"UTC\" (as the page gives it), \"utc_offset_hours\": number (the net's local UTC"
    " offset today, daylight saving included, e.g. -5 for US Central in summer), \"duration_min\": number or null,"
    " \"ncs\": str or null, \"area\": str (town or region it serves), \"lat\": number or null, \"lon\": number or"
    " null (the net's home town or net control's town, decimal degrees), \"distance_km\": number or null (from the"
    " station to the net's home area or net control), \"serves_station_area\": bool (the net's region includes the"
    " station: a state, multi-state or national net), \"kind\": one of emergency traffic social technical special,"
    " \"emergency\": bool, \"source_url\": str, \"org\": str or null (the club or group that runs it),"
    " \"note\": str (short: e.g. 'linked system', 'last listed 2024')}")


def _prompt(loc: dict, want: dict, km: int, hf_km: int, modes: list[str], hf: dict | None = None,
            interests: list[str] | None = None) -> str:
    """One search, one band group: VHF/UHF (want VU) or HF (want HF). Both at once ask twice, in parallel."""
    head = ("You find amateur radio nets for a listening station near " + _where_words(loc) + ". Its radios do: "
            + ", ".join(modes) + ". Only list a net you found on a page, with that page's URL; never guess a frequency, "
            "time or tone. Prefer pages updated in the last two years.\n")
    if want.get("VU") and not want.get("HF"):
        return head + (
            f"Find VHF/UHF nets (2 m, 1.25 m, 70 cm repeaters or simplex) whose repeater or meeting place is within "
            f"{km} km. Work like a local ham would: first find the organizations serving the area (amateur radio "
            "clubs, county ARES groups, RACES / county emergency management radio groups, Skywarn spotter groups, "
            "repeater associations, CERT and hospital radio groups), then look up each one's net schedule on its own "
            "site, newsletter, calendar or social page. Also check repeater directories (RepeaterBook, state repeater "
            "councils) for repeaters noting a net, and linked-repeater systems that reach the area.\n") + _FIELDS % 30
    ints = [i for i in (interests or DEFAULT_INTERESTS) if i in INTERESTS] or DEFAULT_INTERESTS
    ant = hfprop.ANTENNAS.get((hf or {}).get("antenna"), hfprop.ANTENNAS["efhw"])[0]
    return head + (
        f"Find HF nets (160-10 m, voice first) this station could join: nets whose net control or home area is within "
        f"about {hf_km} km, and state, regional and national nets whose coverage includes the station. Kinds wanted: "
        + "; ".join(INTERESTS[i][1] for i in ints) + ".\n"
        "Search widely, the way an experienced HF operator would: the ARRL Net Directory, the ARRL section pages of the "
        "station's state and the states around it (section ARES nets, section traffic nets), NTS region and area net "
        "schedules, state and regional emergency HF nets, 60 m emergency interoperability nets, club and "
        "special-interest group pages, and recent net lists on ham forums. Give the net's own frequency (the "
        "published one, in MHz) and its start time as published.\n"
        f"The station's HF setup: {ant}, {(hf or {}).get('height_m', 10):g} m up, {(hf or {}).get('power_w', 100):g} W: "
        "include nets on 160, 80, 60 and 40 m for regional coverage and 20 m and up for long distances.\n") + _FIELDS % 40


def _post(key: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request("https://api.x.ai/v1/responses", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _text_and_sources(d: dict) -> tuple[str, set[str]]:
    text, seen = "", set()
    for o in d.get("output") or []:
        if o.get("type") == "web_search_call":
            a = o.get("action") or {}
            if a.get("url"):
                seen.add(_norm_url(a["url"]))
            for s in a.get("sources") or []:
                if s.get("url"):
                    seen.add(_norm_url(s["url"]))
        if o.get("type") == "message":
            for c in o.get("content") or []:
                if c.get("type") == "output_text":
                    text += c.get("text") or ""
                    for an in c.get("annotations") or []:
                        if an.get("url"):
                            seen.add(_norm_url(an["url"]))
    return text, seen


def _norm_url(u: str) -> str:
    u = str(u or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u).split("#")[0]
    return u.rstrip("/")


def _json_of(text: str):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    i = min([x for x in (raw.find("{"), raw.find("[")) if x >= 0], default=-1)
    if i < 0:
        raise ValueError("the search gave no list")
    raw = raw[i:]
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(raw)
    return obj.get("nets") if isinstance(obj, dict) else obj


def _num(v):
    try:
        f = float(str(v).replace("MHz", "").replace("Hz", "").replace("+", "").strip())
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _to_pc(days: list[str], hhmm: str, offset_h: float = 0.0) -> tuple[list[str], str]:
    """A net's days and time, given at UTC+offset_h (0 = UTC), as this PC's local days and time (today's offsets: no
    time-zone database is needed, and both ends move together at a daylight-saving change)."""
    h, m = map(int, hhmm.split(":"))
    tz = timezone(timedelta(hours=offset_h))
    out = []
    t = None
    today = date.today()
    for d in days:
        k = DAYS.index(d)
        day = today + timedelta(days=(k - today.weekday()) % 7)
        loc = datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone()
        out.append(DAYS[loc.weekday()])
        t = loc.strftime("%H:%M")
    return out, t or hhmm


def _distance_km(a, b) -> float | None:
    if None in (a[0], a[1], b[0], b[1]):
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _clean(raw: dict, seen: set[str], want: dict, km: int, hf_km: int, modes: list[str],
           home: dict | None = None) -> tuple[dict | None, str]:
    """One model row -> a net the page can offer, or (None, why it was left out)."""
    if not isinstance(raw, dict):
        return None, "not a net"
    name = " ".join(str(raw.get("name") or "").split())[:80]
    f = _num(raw.get("freq_mhz"))
    src = str(raw.get("source_url") or "").strip()
    start = str(raw.get("start") or "").strip()
    if not name:
        return None, "no name"
    if not f or f <= 0:
        return None, f"{name}: no frequency"
    if not src.lower().startswith(("http://", "https://")):
        return None, f"{name}: no source page"
    try:
        start = recur.hhmm(start)
    except ValueError:
        return None, f"{name}: no start time"
    band = _rng(f)
    if band == "HF" and not want.get("HF") or band in ("VHF", "UHF") and not want.get("VU"):
        return None, f"{name}: {band}, not a band your radios do"
    mode = str(raw.get("mode") or "").upper().strip() or ("FM" if band != "HF" else "")
    if re.search(r"DMR|D-?STAR|FUSION|C4FM|YSF|P25|NXDN|M17|DIGITAL VOICE", mode + " " + name.upper()):
        return None, f"{name}: digital voice, which the station can't record or transcribe"
    mode = {"SSB": "LSB" if f < 10 else "USB", "DIGITAL": "DIG", "PSK": "DIG", "FT8": "DIG"}.get(mode, mode)
    if mode not in MODES:
        mode = "FM" if band != "HF" else ("LSB" if f < 10 else "USB")
    if mode not in modes:
        return None, f"{name}: {mode}, which your radios don't do"
    rep = str(raw.get("repeat") or "weekly").lower()
    days = [d[:3].title() for d in (raw.get("weekdays") or []) if str(d)[:3].title() in DAYS]
    if rep == "daily":
        rep, days = "weekly", list(DAYS)
    if not days:
        return None, f"{name}: no day given"
    nth = [int(x) for x in (raw.get("nth") or []) if str(x).lstrip("-").isdigit() and int(x) in (1, 2, 3, 4, 5, -1)]
    if rep == "monthly" and not nth:
        return None, f"{name}: monthly but no week given"
    tz = "UTC" if str(raw.get("time_zone") or "").upper() in ("UTC", "Z", "GMT", "ZULU") else "local"
    off = 0.0 if tz == "UTC" else _num(raw.get("utc_offset_hours"))
    moved = False
    if off is not None and -12 <= off <= 14:
        here = datetime.now().astimezone().utcoffset().total_seconds() / 3600
        if tz == "UTC" or abs(off - here) > 0.01:
            days2, start2 = _to_pc(days, start, off)
            moved = (days2, start2) != (days, start)
            days, start = days2, start2
    dist, dist_src = _num(raw.get("distance_km")), "search"
    nlat, nlon = _num(raw.get("lat")), _num(raw.get("lon"))
    if home and home.get("lat") is not None and nlat is not None and nlon is not None \
            and -90 <= nlat <= 90 and -180 <= nlon <= 180 and (nlat, nlon) != (0, 0):
        dist, dist_src = _distance_km((home["lat"], home["lon"]), (nlat, nlon)), "computed"   # great circle from here
    limit = hf_km if band == "HF" else km
    wide = band == "HF" and bool(raw.get("serves_station_area"))       # a state / regional / national net
    if dist is not None and dist > limit * 1.25 and not wide:
        return None, f"{name}: about {round(dist)} km away"
    dur = _num(raw.get("duration_min"))
    repeat = {"freq": "monthly", "nth": nth, "weekdays": days[:1]} if rep == "monthly" else \
        {"freq": "weekly", "weekdays": sorted(set(days), key=DAYS.index)}
    net = {"name": name, "freq_mhz": round(f, 4), "mode": mode, "start": start,
           "duration_min": int(dur) if dur and 5 <= dur <= 300 else 60, "repeat": repeat,
           "offset_mhz": _num(raw.get("offset_mhz")) if band != "HF" else None,
           "tone_hz": _num(raw.get("tone_hz")) if band != "HF" else None,
           "ncs": (str(raw.get("ncs") or "").strip()[:40] or None), "emergency": bool(raw.get("emergency")),
           "band": band, "area": str(raw.get("area") or "").strip()[:80], "distance_km": round(dist) if dist else None,
           "dist_src": dist_src if dist else None,
           "lat": nlat if dist_src == "computed" else None, "lon": nlon if dist_src == "computed" else None,
           "source_url": src[:400], "source_opened": _norm_url(src) in seen, "utc": tz == "UTC",
           "zone_moved": moved and tz != "UTC",
           "org": str(raw.get("org") or "").strip()[:80] or None, "wide": wide,
           "kind": str(raw.get("kind") or "").lower() if str(raw.get("kind") or "").lower() in INTERESTS else None, "note": str(raw.get("note") or "").strip()[:120]}
    if net["tone_hz"] is not None and not 60 <= net["tone_hz"] <= 260:
        net["tone_hz"] = None
    if net["offset_mhz"] is not None and not -10 <= net["offset_mhz"] <= 10:
        net["offset_mhz"] = None
    import hashlib
    net["id"] = "f" + hashlib.sha1(json.dumps([name.casefold(), net["freq_mhz"], start, days]).encode()).hexdigest()[:8]
    return net, ""


def _next_meeting(n: dict, now: datetime):
    """The net's next start, as an aware UTC time (its time is this PC's local time)."""
    try:
        occ = recur.occurrences({"start": n["start"], "duration_min": n.get("duration_min", 60),
                                 "repeat": n["repeat"]}, now, now + timedelta(days=70))
    except Exception:  # noqa: BLE001
        return None
    nxt = next((o["start"] for o in occ if o["start"] >= now), None)
    return nxt.astimezone(timezone.utc) if nxt else None


def _midpoint(a: tuple, b: tuple) -> tuple:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    bx, by = math.cos(la2) * math.cos(lo2 - lo1), math.cos(la2) * math.sin(lo2 - lo1)
    la = math.atan2(math.sin(la1) + math.sin(la2), math.hypot(math.cos(la1) + bx, by))
    return math.degrees(la), math.degrees(lo1 + math.atan2(by, math.cos(la1) + bx))


def _mark(st, nets: list[dict], hf: dict | None = None, sfi: float = hfprop.TYPICAL_SFI) -> list[dict]:
    """Each found net: already on the schedule?  which radio here would record it?  HF: will this setup reach it?"""
    hf = hf or {"antenna": hfprop.DEFAULT["antenna"], "height_m": hfprop.DEFAULT["height_m"], "power_w": _power_default(st),
                "ground": hfprop.DEFAULT["ground"]}
    have = st.cfg.get("schedule") or []
    home = location(st) or {}
    now = datetime.now()
    for n in nets:
        dup = next((h for h in have if h.get("freq_mhz") and abs(float(h["freq_mhz"]) - n["freq_mhz"]) < 0.002
                    and h.get("start") == n["start"]), None) or \
            next((h for h in have if str(h.get("name") or "").casefold() == n["name"].casefold()), None)
        n["have"] = dup["name"] if dup else None
        best, why = None, ""
        for kind in ("ready", "tune", "cant"):
            for r in st.radios.values():
                fz = fit.fit(n, r)
                if fz["kind"] == kind and (kind != "cant" or fz.get("antenna")):
                    best, why = r["id"], fz["why"]
                    break
            if best:
                break
        n["radio"], n["radio_why"] = best, why
        if n.get("band") == "HF":
            when = _next_meeting(n, now)                       # the sun over the path when the net next meets
            mid = None
            if home.get("lat") is not None:
                here = (home["lat"], home["lon"])
                mid = _midpoint(here, (n["lat"], n["lon"])) if n.get("lat") is not None else here
            n["prop"] = hfprop.estimate(n["freq_mhz"], n.get("distance_km"), n["start"], hf["antenna"], hf["height_m"],
                                        hf["power_w"], sfi, tuple(hf["good_km"]) if hf.get("good_km") else None,
                                        when, mid, hf.get("ground") or "average")
    return nets


class Finder:
    def __init__(self, st, post=None, fetch=None):
        self.st = st
        self.post = post or _post
        self.fetch = fetch                                      # NOAA's solar flux (hfprop.solar_flux); None = the web
        self.lock = threading.Lock()
        self.busy = False

    def key(self):
        from . import secrets
        return secrets.read(self.st.root, "XAI_API_KEY")

    def _sfi(self, d: dict) -> tuple[float, str]:
        c = dict(d.get("sfi") or {})
        v, src = hfprop.solar_flux(c, self.fetch)
        if c != (d.get("sfi") or {}):
            with self.lock:
                dd = _load(self.st)
                dd["sfi"] = c
                _save(self.st, dd)
        return v, src

    def state(self) -> dict:
        d = _load(self.st)
        last = d.get("last") or {}
        if last.get("at") and time.time() - last["at"] > KEEP_S:
            last = {}
        hf = self._hf(d)
        sfi, sfi_src = self._sfi(d) if any(n.get("band") == "HF" for n in last.get("nets") or []) or reach(self.st)["HF"] \
            else (hfprop.TYPICAL_SFI, "typical")
        nets = _mark(self.st, [dict(n) for n in last.get("nets") or []], hf, sfi)
        have = [n for n in nets if n["have"]]                   # owner 11-14: only nets not already scheduled are shown
        nets = [n for n in nets if not n["have"]]
        order = {"likely": 0, "maybe": 1, "unlikely": 2}
        nets.sort(key=lambda n: (n["band"] == "HF", order.get((n.get("prop") or {}).get("chance"), 0),
                                 n["distance_km"] if n["distance_km"] is not None else 9999, n["name"].casefold()))
        return {"key": bool(self.key()), "location": location(self.st), "km": d.get("km", DEFAULT_KM),
                "hf_km": d.get("hf_km", DEFAULT_HF_KM), "reach": reach(self.st), "hf": hf,
                "antennas": [{"id": k, "label": v[0], "kind": v[1]} for k, v in hfprop.ANTENNAS.items()],
                "grounds": [{"id": k, "label": v[0]} for k, v in hfprop.GROUNDS.items()],
                "interests": [{"id": k, "label": v[0], "on": k in self._interests(d)} for k, v in INTERESTS.items()],
                "sfi": {"value": round(sfi), "source": sfi_src}, "address": d.get("address") or "",
                "busy": self.busy, "at": last.get("at"), "nets": nets,
                "scheduled": [{"name": n["name"], "as": n["have"]} for n in have], "left_out": last.get("left_out") or [],
                "error": last.get("error"), "cost_usd": last.get("cost_usd"), "searched": last.get("searched"),
                "by_band": last.get("by_band") or {}}

    def _interests(self, d: dict) -> list[str]:
        v = [i for i in d.get("interests") or [] if i in INTERESTS]
        return v or list(DEFAULT_INTERESTS)

    def _hf(self, d: dict) -> dict:
        h = dict(d.get("hf") or {})
        if h.get("antenna") in hfprop.OLD_IDS:
            h["antenna"], h["height_m"] = hfprop.OLD_IDS[h["antenna"]]
        return {"antenna": h.get("antenna") if h.get("antenna") in hfprop.ANTENNAS else hfprop.DEFAULT["antenna"],
                "height_m": h.get("height_m") or hfprop.DEFAULT["height_m"],
                "power_w": h.get("power_w") or _power_default(self.st), "good_km": h.get("good_km"),
                "ground": h.get("ground") if h.get("ground") in hfprop.GROUNDS else hfprop.DEFAULT["ground"], "set": bool(h)}

    def settings(self, body: dict) -> dict:
        with self.lock:
            d = _load(self.st)
            if "km" in body:
                km = _num(body.get("km"))
                if not km or not 1 <= km <= 500:
                    raise ValueError("distance must be 1 to 500 km")
                d["km"] = int(km)
            if "hf_km" in body:
                km = _num(body.get("hf_km"))
                if not km or not 50 <= km <= 5000:
                    raise ValueError("HF distance must be 50 to 5000 km")
                d["hf_km"] = int(km)
            if "hf" in body:
                h = body.get("hf") or {}
                if h.get("antenna") not in hfprop.ANTENNAS:
                    raise ValueError("choose the HF antenna")
                p = _num(h.get("power_w"))
                if not p or not 1 <= p <= 1500:
                    raise ValueError("HF power must be 1 to 1500 W")
                ht = _num(h.get("height_m")) if h.get("height_m") not in (None, "") else hfprop.DEFAULT["height_m"]
                if ht is None or not 0 <= ht <= 100:
                    raise ValueError("antenna height must be 0 to 100 m")
                good = None
                lo, hi = _num(h.get("good_from_km")), _num(h.get("good_to_km"))
                if lo or hi:
                    if not (lo and hi and 20 <= lo < hi <= 4000):
                        raise ValueError("where you hear HF stations well: give both distances, like 400 to 600 km")
                    good = [int(lo), int(hi)]
                gr = h.get("ground") if h.get("ground") in hfprop.GROUNDS else hfprop.DEFAULT["ground"]
                d["hf"] = {"antenna": h["antenna"], "height_m": round(ht, 1), "power_w": int(p), "good_km": good,
                           "ground": gr}
            if "interests" in body:
                v = [i for i in body.get("interests") or [] if i in INTERESTS]
                if not v:
                    raise ValueError("choose at least one kind of HF net")
                d["interests"] = v
            if "address" in body:
                d["address"] = " ".join(str(body.get("address") or "").split())[:200]
            if "gps" in body:
                g = body.get("gps")
                if g is None:
                    d.pop("gps", None)
                else:
                    lat, lon = _num((g or {}).get("lat")), _num((g or {}).get("lon"))
                    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("that location is not a latitude and longitude")
                    acc = _num(g.get("acc_m"))
                    d["gps"] = {"lat": round(lat, 3), "lon": round(lon, 3), "acc_m": int(acc) if acc else None}
            _save(self.st, d)
        return self.state()

    def _one(self, key, loc, group, km, hf_km, modes, hf, interests) -> dict:
        """One web search for one band group ("VU" or "HF"): {"rows", "seen", "cost", "error"}."""
        want = {"VU": group == "VU", "HF": group == "HF"}
        body = {"model": MODEL, "max_tool_calls": 30 if group == "HF" else 24, "tools": [{"type": "web_search"}],
                "input": [{"role": "user", "content": _prompt(loc, want, km, hf_km, modes, hf, interests)}]}
        try:
            resp = self.post(key, body, TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            return {"rows": [], "seen": set(), "cost": None, "error": f"{getattr(e, 'code', None) or type(e).__name__}"}
        text, seen = _text_and_sources(resp)
        try:
            rows = _json_of(text) or []
        except ValueError:
            rows = []
        ticks = (resp.get("usage") or {}).get("cost_in_usd_ticks")
        return {"rows": rows if isinstance(rows, list) else [], "seen": seen,
                "cost": ticks / 1e10 if isinstance(ticks, (int, float)) else None, "error": None}

    def search(self, bands: str | None = None, body: dict | None = None) -> dict:
        """Search now. `body` may carry the page's current settings (km, hf_km, hf, interests): saved first, so a
        field still being edited is never lost to the click. Both band groups: two searches in parallel; one group:
        the other group's earlier results are kept."""
        body = {k: v for k, v in (body or {}).items() if k in ("km", "hf_km", "hf", "interests")}
        if body:
            self.settings(body)
        key = self.key()
        if not key:
            raise ValueError("set the xAI key first (Settings -> Station agent)")
        loc = location(self.st)
        if not loc:
            raise ValueError("say where the station is: a grid square in Settings, this PC's location, or an address")
        r = reach(self.st)
        groups = [g for g, ok in (("VU", r["VU"]), ("HF", r["HF"])) if ok and bands in (None, "", g)]
        if not groups:
            raise ValueError("no radio here records " + ({"HF": "HF", "VU": "VHF/UHF"}.get(bands or "", "HF or VHF/UHF"))
                             + " nets: add a radio (or set its type in Radios) first")
        d = _load(self.st)
        km, hf_km, hf, ints = int(d.get("km", DEFAULT_KM)), int(d.get("hf_km", DEFAULT_HF_KM)), self._hf(d), self._interests(d)
        with self.lock:
            if self.busy:
                raise ValueError("a search is already running")
            self.busy = True
        try:
            res: dict[str, dict] = {}
            ths = [threading.Thread(target=lambda g=g: res.__setitem__(g, self._one(key, loc, g, km, hf_km, r["modes"],
                                                                                      hf, ints)), daemon=True)
                   for g in groups]
            for t in ths:
                t.start()
            for t in ths:
                t.join(TIMEOUT_S + 10)
            prev = (d.get("last") or {}) if time.time() - (d.get("last") or {}).get("at", 0) < KEEP_S else {}
            by_band = dict(prev.get("by_band") or {})
            nets = [n for n in prev.get("nets") or [] if ("HF" if n.get("band") == "HF" else "VU") not in groups]
            out = [x for x in prev.get("left_out_by") or [] if x.get("g") not in groups]
            ids = {n["id"] for n in nets}
            cost, errors = 0.0, []
            for g in groups:
                one = res.get(g) or {"rows": [], "seen": set(), "cost": None, "error": "timed out"}
                want = {"VU": g == "VU", "HF": g == "HF"}
                kept = 0
                for raw in one["rows"]:
                    b = _rng((raw or {}).get("freq_mhz") if isinstance(raw, dict) else None)
                    if b and ("HF" if b == "HF" else "VU") != g:
                        continue                                # the other group's search covers it
                    n, why = _clean(raw, one["seen"], want, km, hf_km, r["modes"], loc)
                    if n and n["id"] not in ids:
                        ids.add(n["id"])
                        nets.append(n)
                        kept += 1
                    elif why:
                        out.append({"g": g, "why": why})
                cost += one["cost"] or 0.0
                if one["error"]:
                    errors.append(f"the {'HF' if g == 'HF' else 'VHF/UHF'} search failed ({one['error']})")
                by_band[g] = {"at": time.time(), "found": len(one["rows"]), "kept": kept, "error": one["error"],
                              "km": hf_km if g == "HF" else km,
                              "interests": [INTERESTS[i][0] for i in ints] if g == "HF" else None}
                print(f"netfind: {g} search: {len(one['rows'])} found, {kept} kept, ${(one['cost'] or 0):.3f}"
                      + (f", error {one['error']}" if one["error"] else ""), flush=True)
            last = {"at": time.time(), "nets": nets, "left_out_by": out[:80], "left_out": [x["why"] for x in out][:80],
                    "cost_usd": round(cost, 3), "by_band": by_band,
                    "searched": {"where": loc.get("label"), "km": km, "hf_km": hf_km,
                                 "bands": [{"VU": "VHF/UHF", "HF": "HF"}[g] for g in groups]},
                    "error": "; ".join(errors) or (None if nets else "the search found no nets with a source page")}
            with self.lock:
                d = _load(self.st)
                d["last"] = last
                _save(self.st, d)
        finally:
            self.busy = False
        if errors and not any(by_band[g]["kept"] for g in groups):
            raise ValueError("; ".join(errors) + ": try again in a minute")
        return self.state()

    def add(self, ids, radios: dict | None = None) -> dict:
        """Add the ticked nets. radios = {found id: radio id or ""} overrides the suggestion."""
        want = [str(i) for i in ids or []]
        if not want:
            raise ValueError("tick the nets to add")
        found = {n["id"]: n for n in (_load(self.st).get("last") or {}).get("nets") or []}
        found = {n["id"]: n for n in _mark(self.st, list(found.values()), self._hf(_load(self.st)))}
        added, failed = [], []
        for i in want:
            n = found.get(i)
            if not n:
                failed.append({"name": i, "why": "no longer in the list: search again"})
                continue
            rid = (radios or {}).get(i, n.get("radio")) or None
            body = {k: n[k] for k in ("name", "freq_mhz", "mode", "start", "duration_min", "repeat", "offset_mhz",
                                      "tone_hz", "ncs", "emergency") if n.get(k) is not None}
            body["radio"] = rid
            body["source"] = n["source_url"]
            try:
                self.st.nets.add(body)
                added.append(n["name"])
            except ValueError as e:
                try:                                                    # a field the editor refuses: keep the net
                    self.st.nets.add({k: body[k] for k in ("name", "freq_mhz", "mode", "start", "duration_min",
                                                            "repeat", "radio", "emergency") if k in body})
                    added.append(n["name"])
                except ValueError as e2:
                    failed.append({"name": n["name"], "why": str(e2 or e)})
        return self.state() | {"added": added, "failed": failed, "schedule": self.st.nets.list().get("schedule")}
