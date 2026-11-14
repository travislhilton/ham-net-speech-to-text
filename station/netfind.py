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

# ---------------------------------------------------------------- HF: will this setup reach the net? -------------
# The operator's HF setup (asked on the Find nets page): antenna kind and power. A rough estimate, not a forecast: typical
# mid-cycle conditions, no live solar data. Each antenna: dB against a half-wave dipole, at high angles (short paths,
# NVIS, under ~500 km) and at low angles (long paths).
ANTENNAS = {
    "dipole_low": ("Dipole or inverted V, low (under 1/4 wave up: NVIS)", 0, -4),
    "dipole_high": ("Dipole, high (1/2 wave up or more)", -3, 0),
    "efhw": ("End-fed half wave or random wire", -1, -2),
    "loop": ("Horizontal loop", 1, -1),
    "vertical": ("Vertical (ground-mounted or with radials)", -10, 0),
    "beam": ("Beam / Yagi", -6, 6),
    "whip": ("Mobile whip, compact or portable whip", -12, -10),
}
DEFAULT_ANTENNA = "efhw"


def _power_default(st) -> int:
    for r in st.radios.values():
        if r.get("role") != "winlink" and (r.get("model") or "") == "ic705":
            return 10
    return 100


def propagation(f_mhz: float, km: float | None, start: str, antenna: str, power_w: float) -> dict:
    """{"chance": hear it, "checkin": be heard checking in ("likely" | "maybe" | "unlikely"), "why": words}: an HF net at frequency f, `km` away (None: a regional
    net, taken as 300 km), meeting at local `start`, from this antenna at this power."""
    h = int(start[:2]) if start and start[:2].isdigit() else 19
    day = 8 <= h < 18
    dusk = h in (6, 7, 18, 19, 20)
    d = 300.0 if km is None else float(km)
    short = d < 500
    name, hi, lo = ANTENNAS.get(antenna, ANTENNAS[DEFAULT_ANTENNA])
    band = "160 m" if f_mhz < 2.2 else "80 m" if f_mhz < 4.5 else "60 m" if f_mhz < 6 else "40 m" if f_mhz < 7.5 \
        else "30 m" if f_mhz < 11 else "20 m" if f_mhz < 15 else "17 m" if f_mhz < 19 else "15 m" if f_mhz < 22 \
        else "12 m" if f_mhz < 25 else "10 m"
    # the path itself: does the band carry this distance at this hour?
    if f_mhz < 2.2:
        path, why = (8 if not day and d < 1500 else -12), "160 m works at night, under ~1500 km"
    elif f_mhz < 6:
        if short:
            path, why = (8 if not day or dusk else 4), f"{band} NVIS covers short paths day and night"
        else:
            path, why = (6 if not day else -10), f"{band} reaches {round(d)} km only after dark"
    elif f_mhz < 7.5:
        if short:
            path, why = (8 if day else -2 if dusk else -10), "40 m covers short paths in daylight; it skips over them at night"
        else:
            path, why = (6 if not day or dusk else 2), f"40 m reaches {round(d)} km best evenings and nights"
    elif f_mhz < 11:
        path, why = ((4 if day else -4) if not short else -8), "30 m: daytime, beyond ~500 km"
    else:
        if short:
            path, why = -14, f"{band} skips over stations under ~500 km"
        else:
            path, why = ((6 if day else -6) if f_mhz < 15 else (2 if day else -14)), f"{band} opens in daylight to {round(d)} km and more"
    ant = hi if short else lo
    pwr = 10 * math.log10(max(1.0, float(power_w)) / 100.0)
    word = lambda x: "likely" if x >= 3 else "maybe" if x >= -4 else "unlikely"    # noqa: E731
    hear = path + ant                       # hearing: net control's ~100 W into this antenna; your power plays no part
    send = path + ant + pwr                 # checking in: your power too
    bits = [why, f"{name.split(' (')[0].lower()} {'+' if ant >= 0 else ''}{ant} dB at {'high' if short else 'low'} angles",
            f"checking in with {round(power_w)} W: {'+' if pwr >= 0 else ''}{pwr:.0f} dB against 100 W"]
    return {"chance": word(hear), "checkin": word(send), "why": "; ".join(bits), "band": band}


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


def _prompt(loc: dict, want: dict, km: int, hf_km: int, modes: list[str], hf: dict | None = None) -> str:
    kinds = []
    if want.get("VU"):
        kinds.append(f"VHF/UHF nets (2 m, 1.25 m, 70 cm repeaters or simplex) whose repeater or meeting place is within "
                     f"{km} km")
    if want.get("HF"):
        kinds.append(f"HF nets (160-10 m) a station there could join: regional, state, section and traffic nets, "
                     f"net control within about {hf_km} km")
    return (
        "You find amateur radio nets for a listening station. The station is near " + _where_words(loc) + ".\n"
        "Find " + " and ".join(kinds) + ". The station's radios do these modes: " + ", ".join(modes) + ".\n"
        "Work like a local ham would: first find the organizations serving the area (amateur radio clubs, county "
        "ARES groups, RACES / county emergency management radio groups, Skywarn spotter groups, repeater "
        "associations, the ARRL section and its traffic (NTS) nets), then look up each one's net schedule on its own "
        "site, newsletter or calendar. Also check repeater directories (RepeaterBook and state repeater councils) for "
        "repeaters noting a net" + (", and the state ARES/section pages for HF nets" if want.get("HF") else "") + ". Prefer pages updated in the last two "
        "years. Only list a net you found on a page, with that page's URL; never guess a frequency, time or tone.\n"
        + (f"The station's HF setup: {ANTENNAS[hf['antenna']][0]}, {hf['power_w']} W; favour nets on bands and at "
           "distances that setup can reach.\n" if want.get("HF") and hf else "")
        + "Answer ONLY with a JSON object, no prose: {\"nets\": [ ... up to 30 ... ]} where each net is\n"
        "{\"name\": str, \"freq_mhz\": number (the output/listening frequency), \"mode\": one of FM USB LSB AM CW DIG (skip DMR, D-STAR, Fusion and other digital voice nets),"
        " \"offset_mhz\": number or null (repeater offset, e.g. 0.6 or -5), \"tone_hz\": number or null (CTCSS),"
        " \"repeat\": \"weekly\" | \"monthly\" | \"daily\", \"weekdays\": [\"Mon\"..\"Sun\"],"
        " \"nth\": [1-5 or -1] (monthly only, e.g. 2nd Tuesday = [2]), \"start\": \"HH:MM\" 24-hour,"
        " \"time_zone\": \"local\" | \"UTC\" (as the page gives it), \"utc_offset_hours\": number (the net's local UTC"
        " offset today, daylight saving included, e.g. -5 for US Central in summer), \"duration_min\": number or null,"
        " \"ncs\": str or null, \"area\": str (town or region it serves), \"distance_km\": number or null (from the"
        " station), \"emergency\": bool (ARES, RACES, Skywarn, emergency or EOC net), \"source_url\": str,"
        " \"org\": str or null (the club or group that runs it), \"note\": str (short: e.g. 'linked system', 'last listed 2024')}")


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


def _clean(raw: dict, seen: set[str], want: dict, km: int, hf_km: int, modes: list[str]) -> tuple[dict | None, str]:
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
    dist = _num(raw.get("distance_km"))
    limit = hf_km if band == "HF" else km
    if dist is not None and dist > limit * 1.25:
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
           "source_url": src[:400], "source_opened": _norm_url(src) in seen, "utc": tz == "UTC",
           "zone_moved": moved and tz != "UTC",
           "org": str(raw.get("org") or "").strip()[:80] or None, "note": str(raw.get("note") or "").strip()[:120]}
    if net["tone_hz"] is not None and not 60 <= net["tone_hz"] <= 260:
        net["tone_hz"] = None
    if net["offset_mhz"] is not None and not -10 <= net["offset_mhz"] <= 10:
        net["offset_mhz"] = None
    import hashlib
    net["id"] = "f" + hashlib.sha1(json.dumps([name.casefold(), net["freq_mhz"], start, days]).encode()).hexdigest()[:8]
    return net, ""


def _mark(st, nets: list[dict], hf: dict | None = None) -> list[dict]:
    """Each found net: already on the schedule?  which radio here would record it?  HF: will this setup reach it?"""
    hf = hf or {"antenna": DEFAULT_ANTENNA, "power_w": _power_default(st)}
    have = st.cfg.get("schedule") or []
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
            n["prop"] = propagation(n["freq_mhz"], n.get("distance_km"), n["start"], hf["antenna"], hf["power_w"])
    return nets


class Finder:
    def __init__(self, st, post=None):
        self.st = st
        self.post = post or _post
        self.lock = threading.Lock()
        self.busy = False

    def key(self):
        from . import secrets
        return secrets.read(self.st.root, "XAI_API_KEY")

    def state(self) -> dict:
        d = _load(self.st)
        last = d.get("last") or {}
        if last.get("at") and time.time() - last["at"] > KEEP_S:
            last = {}
        nets = _mark(self.st, [dict(n) for n in last.get("nets") or []], self._hf(d))
        return {"key": bool(self.key()), "location": location(self.st), "km": d.get("km", DEFAULT_KM),
                "hf_km": d.get("hf_km", DEFAULT_HF_KM), "reach": reach(self.st), "hf": self._hf(d),
                "antennas": [{"id": k, "label": v[0]} for k, v in ANTENNAS.items()], "address": d.get("address") or "",
                "busy": self.busy, "at": last.get("at"), "nets": nets, "left_out": last.get("left_out") or [],
                "error": last.get("error"), "cost_usd": last.get("cost_usd"), "searched": last.get("searched")}

    def _hf(self, d: dict) -> dict:
        h = d.get("hf") or {}
        return {"antenna": h.get("antenna") if h.get("antenna") in ANTENNAS else DEFAULT_ANTENNA,
                "power_w": h.get("power_w") or _power_default(self.st), "set": bool(h)}

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
                if h.get("antenna") not in ANTENNAS:
                    raise ValueError("choose the HF antenna")
                p = _num(h.get("power_w"))
                if not p or not 1 <= p <= 1500:
                    raise ValueError("HF power must be 1 to 1500 W")
                d["hf"] = {"antenna": h["antenna"], "power_w": int(p)}
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

    def search(self, bands: str | None = None) -> dict:
        key = self.key()
        if not key:
            raise ValueError("set the xAI key first (Settings -> Station agent)")
        loc = location(self.st)
        if not loc:
            raise ValueError("say where the station is: a grid square in Settings, this PC's location, or an address")
        r = reach(self.st)
        want = {"HF": r["HF"], "VU": r["VU"]}
        if bands == "HF":
            want["VU"] = False
        elif bands == "VU":
            want["HF"] = False
        if not (want["HF"] or want["VU"]):
            raise ValueError("no radio here records HF or VHF/UHF nets: add a radio (or set its type) first")
        d = _load(self.st)
        km, hf_km = int(d.get("km", DEFAULT_KM)), int(d.get("hf_km", DEFAULT_HF_KM))
        with self.lock:
            if self.busy:
                raise ValueError("a search is already running")
            self.busy = True
        try:
            body = {"model": MODEL, "max_tool_calls": 24, "tools": [{"type": "web_search"}],
                    "input": [{"role": "user", "content": _prompt(loc, want, km, hf_km, r["modes"], self._hf(d))}]}
            try:
                resp = self.post(key, body, TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                why = getattr(e, "code", None)
                raise ValueError(f"the search failed ({why or type(e).__name__}); try again in a minute") from None
            text, seen = _text_and_sources(resp)
            try:
                rows = _json_of(text) or []
            except ValueError:
                rows = []
            nets, out, ids = [], [], set()
            for raw in rows if isinstance(rows, list) else []:
                n, why = _clean(raw, seen, want, km, hf_km, r["modes"])
                if n and n["id"] not in ids:
                    ids.add(n["id"])
                    nets.append(n)
                elif why:
                    out.append(why)
            nets.sort(key=lambda n: (n["band"] == "HF", n["distance_km"] if n["distance_km"] is not None else 9999,
                                     n["name"].casefold()))
            ticks = (resp.get("usage") or {}).get("cost_in_usd_ticks")
            cost = round(ticks / 1e10, 3) if isinstance(ticks, (int, float)) else None
            last = {"at": time.time(), "nets": nets, "left_out": out[:40], "cost_usd": cost,
                    "searched": {"where": loc.get("label"), "km": km, "hf_km": hf_km,
                                 "bands": [b for b, ok in (("VHF/UHF", want["VU"]), ("HF", want["HF"])) if ok]},
                    "error": None if nets or rows else "the search found no nets with a source page"}
            with self.lock:
                d = _load(self.st)
                d["last"] = last
                _save(self.st, d)
        finally:
            self.busy = False
        return self.state()

    def add(self, ids, radios: dict | None = None) -> dict:
        """Add the ticked nets. radios = {found id: radio id or ""} overrides the suggestion."""
        want = [str(i) for i in ids or []]
        if not want:
            raise ValueError("tick the nets to add")
        found = {n["id"]: n for n in (_load(self.st).get("last") or {}).get("nets") or []}
        found = {n["id"]: n for n in _mark(self.st, list(found.values()))}
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
