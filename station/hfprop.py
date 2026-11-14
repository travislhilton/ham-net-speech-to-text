"""Will this HF setup hear a net, and be heard checking in? (Nets -> Find nets; station/netfind.py)

A small sky-wave model, not a forecast. It works out, for the net's frequency, distance and local hour:

  the path    hops of at most ~3,800 km off the F2 layer (300 km up); each hop's takeoff angle from geometry
  the layer   foF2 from the solar flux (NOAA's daily 10.7 cm flux when the PC is online, else a typical 130) and the
              hour (mid-latitudes; season ignored); the MUF = foF2 x the secant of the angle the wave meets the layer
  the losses  free-space spreading over the slanted path, D-layer absorption by day (falls with 1/(f+1.4)^2, grows with
              a low angle), ground reflections between hops, a fixed excess loss, and the band's noise
  the antenna its gain AT THAT TAKEOFF ANGLE: a horizontal antenna's pattern from its height in wavelengths over ground
              (a low dipole fires straight up: NVIS; a high one fires low: distance), a vertical's low-angle pattern
              and ground losses, a whip's poor efficiency on the low bands, a beam only on 20-10 m

"Hear": net control's 100 W into a dipole at 15 m, into this antenna (on HF a weak antenna also hears less band noise,
so a poor receive antenna costs less than it does on transmit). "Check in": this station's power and antenna, heard on
net control's dipole. SNR 12 dB and up = likely, 3-12 = maybe, below = unlikely; the percentage allows for conditions
varying several dB from night to night.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request

R = 6371.0
H_F2 = 300.0
H_D = 80.0
MIN_TAKEOFF = 3.0
EXCESS_DB = 30.0             # polarization, defocusing, deviative losses: calibrates 100 W dipoles over 500 km at night to ~S8
NCS = {"antenna": "dipole", "height_m": 15.0, "power_w": 100.0}
TYPICAL_SFI = 130.0

# Antenna figures (researched 2026-11-14; NEC-4 models by L. B. Cebik, ARRL Antenna Book, N6LF's QEX ground-system
# measurements, Zavrel QEX 2016, 49:1 transformer loss measurements): every gain is dBi averaged over azimuth.
#   horizontal wires: direct + ground-reflected wave (reflection 0.75 below 8 MHz, 0.85 from 14 MHz; real ground never
#     cancels fully: a 0.04-wave dipole still gives ~-0.5 dBi straight up). Calibrated: dipole 0.2 wave up ~6.4 dBi at
#     the zenith; 0.5 wave ~7 dBi at 28 deg; 1 wave ~8 dBi at 14 deg.
#   inverted V: a dipole at ~0.8 x apex height, -0.7 dB, its end nulls filled (azimuth -1 dB, not -2)
#   end-fed half wave: a dipole at its mean height, 49:1 transformer and feed -1.5 dB (-2 on 80 m)
#   random / long wire + tuner: dipole-like at its height, -3 dB (-5 on 80 m) tuner and coax losses
#   horizontal loop: +0.8 dB over a dipole, nearly round (-0.5); NVIS dipole + reflector: +0.5 dB
#   vertical (16+ radials): peak 0 / +1.5 / +4.5 dBi at 26 / 18 / 5 deg over average / good / salt water ground
#     (poor: -0.5 dBi at 29 deg); 0.25-0.35 dB per degree lost below the peak; cos above; nothing straight up
#   vertical delta loop (corner fed): a vertical pattern, +1.5 dB, peak ~20 deg
#   whip: a vertical pattern with -17 / -11 / -4 dB efficiency on 80 / 40 / 20 m (8 ft class, mobile), a car body
#     still fills high angles to 10 dB under the peak
#   beam: 3-element Yagi 7.7 dBi free space (hex beam 6.5), aimed at the net; useless below 20 m
# id: (label, kind, free-space gain dBi, overall adjustment dB, azimuth penalty dB at low angles)
ANTENNAS = {
    "dipole": ("Dipole (flat)", "h", 2.15, 0.0, -2.0),
    "invv": ("Inverted V", "h", 2.15, -0.7, -1.0),
    "efhw": ("End-fed half wave", "h", 2.15, -1.5, -2.0),
    "random": ("Random or long wire with a tuner", "h", 2.15, -3.0, -1.0),
    "loop": ("Horizontal loop (skyloop)", "h", 2.95, 0.0, -0.5),
    "nvis": ("NVIS antenna (low dipole over a reflector)", "h", 2.15, 0.5, -0.5),
    "vertical": ("Vertical with radials", "v", 0.0, -1.0, 0.0),
    "delta": ("Vertical delta loop or quad (corner fed)", "v", 1.5, 0.0, -1.0),
    "beam": ("Beam / Yagi (20-10 m)", "h", 7.7, 0.0, 0.0),
    "hexbeam": ("Hex beam (20-6 m)", "h", 6.5, 0.0, 0.0),
    "whip": ("Mobile or portable whip", "w", 0.0, -1.0, 0.0),
}
GROUNDS = {"poor": ("Poor (dry, rocky, city)", -0.5, 29.0, 0.35), "average": ("Average", 0.0, 26.0, 0.25),
           "good": ("Good (moist farmland)", 1.5, 18.0, 0.2), "salt": ("Salt water", 4.5, 5.0, 0.05)}
DEFAULT = {"antenna": "efhw", "height_m": 10.0, "ground": "average"}
OLD_IDS = {"dipole_low": ("dipole", 6.0), "dipole_high": ("dipole", 20.0)}   # the first Find nets build's choices


def band_of(f: float) -> str:
    return "160 m" if f < 2.2 else "80 m" if f < 4.5 else "60 m" if f < 6 else "40 m" if f < 7.5 else "30 m" \
        if f < 11 else "20 m" if f < 15 else "17 m" if f < 19 else "15 m" if f < 22 else "12 m" if f < 25 else "10 m"


# ---------------------------------------------------------------- the antenna -----------------------------------
def _rho(f: float) -> float:
    return 0.75 if f < 8 else 0.85 if f >= 14 else 0.75 + 0.1 * (f - 8) / 6


def _whip_eff(f: float) -> float:
    """dB: an 8 ft class mobile whip, -17 / -11 / -4 dB at 3.9 / 7.2 / 14.2 MHz (log-interpolated)."""
    pts = ((1.9, -24.0), (3.9, -17.0), (7.2, -11.0), (14.2, -4.0), (28.5, -1.0))
    if f <= pts[0][0]:
        return pts[0][1]
    for (f1, g1), (f2, g2) in zip(pts, pts[1:]):
        if f <= f2:
            return g1 + (g2 - g1) * math.log(f / f1) / math.log(f2 / f1)
    return pts[-1][1]


def gain(antenna: str, f: float, height_m: float, theta: float, ground: str = "average") -> float:
    """dBi at elevation `theta` degrees, averaged over azimuth (the net's direction is not known; a beam is aimed)."""
    _, kind, g0, adj, az = ANTENNAS.get(antenna, ANTENNAS[DEFAULT["antenna"]])
    lam = 300.0 / f
    deg = max(0.5, min(90.0, theta))
    th = math.radians(deg)
    if antenna in ("beam", "hexbeam") and f < 13.9:
        return -25.0                                               # a 20-10 m beam is no antenna on 40 or 80 m
    azp = az * max(0.0, min(1.0, (60.0 - deg) / 45.0))             # end nulls matter at low angles, not overhead
    if kind == "h":                                                # horizontal over ground: direct + reflected wave
        h = max(1.0, height_m) * (0.8 if antenna == "invv" else 1.0)
        rho = _rho(f)
        h += (0.03 if f < 8 else 0.02) * lam                       # real ground reflects from a little below it
        ph = 4 * math.pi * h / lam * math.sin(th)
        af_db = 10 * math.log10(max(1e-6, 1 + rho * rho - 2 * rho * math.cos(ph)))
        af_db = max(af_db, -2.7) if deg > 45 else af_db            # real ground: a very low wire still radiates up
        extra = (-0.5 if f < 4.5 else 0.0) if antenna == "efhw" else (-2.0 if f < 4.5 else 0.0) if antenna == "random" else 0.0
        return g0 + adj + extra + af_db + azp
    gname, gpk, tpk, roll = GROUNDS.get(ground, GROUNDS["average"])
    if antenna == "delta":
        tpk = min(tpk, 20.0)
    peak = gpk + g0 + adj
    if deg >= tpk:
        g = peak + 20 * math.log10(max(0.03, math.cos(th) / math.cos(math.radians(tpk))))
    else:
        g = peak - roll * (tpk - deg)
    if kind == "w":
        g = max(g, peak - 10.0) + _whip_eff(f)                     # the car body fills high angles
    return g + azp


def measured_gain(good_km: tuple[float, float], theta: float) -> float:
    """The operator's own antenna, from where they hear stations well ("hops of 400-600 km"): full gain between the
    takeoff angles of those hop lengths, falling off above (too steep: the skip zone close in) and below (too low)."""
    lo, hi = sorted(good_km)
    t_hi, t_lo = takeoff(hi), takeoff(lo)                         # longer hop = lower angle
    g = 2.0
    if theta > t_lo:
        g -= 0.8 * (theta - t_lo)                                 # steeper than the sweet spot: falls fast
    elif theta < t_hi:
        g -= 0.6 * (t_hi - theta)
    return max(-25.0, g)


# ---------------------------------------------------------------- the ionosphere ----------------------------------
def _day(hour: float) -> float:
    return max(0.0, math.sin(math.pi * (hour - 6.0) / 13.0))      # 0 at night, 1 early afternoon (06:00-19:00)


def _f2_day(hour: float) -> float:
    """The F2 layer builds after sunrise and decays for hours after sunset (unlike the D layer, gone at dusk)."""
    h = hour % 24
    if 6 <= h <= 19:
        return _day(h) ** 0.7
    after = (h - 19) if h > 19 else (h + 5)                       # hours since 19:00
    return 0.45 * math.exp(-after / 2.5)


def fof2(hour: float, sfi: float, sun: dict | None = None) -> float:
    night, day = 2.2 + 0.012 * sfi, 3.5 + 0.035 * sfi
    if sun:                                                       # the real sun over the path, and the season
        night, day = night * sun["night_x"], day * sun["day_x"]
        return night + (day - night) * sun["f2"]
    return night + (day - night) * _f2_day(hour)


# ---------------------------------------------------------------- the sun over the path, on the net's date ---------
def solar_zenith(when_utc, lat: float, lon: float) -> float:
    """Degrees from straight up (NOAA's low-precision formula: good to a fraction of a degree)."""
    doy = when_utc.timetuple().tm_yday
    hr = when_utc.hour + when_utc.minute / 60
    g = 2 * math.pi / 365 * (doy - 1 + (hr - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g)
                    - 0.040849 * math.sin(2 * g))
    tst = hr * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    la = math.radians(lat)
    c = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def sun_over(when_utc, lat: float, lon: float) -> dict:
    """What the sun does to the layers at the path's midpoint at the net's time:
    d     D-layer absorption factor: cos(zenith)^0.75 while the sun is up, ~0 after dusk
    f2    F2 ionization factor, from the sun 1.5 h earlier (the layer lags the sun) and decaying for hours after dusk
    day_x / night_x  season: mid-latitude winter days run higher and winter nights lower than summer (the winter anomaly)
    """
    from datetime import timedelta
    z = solar_zenith(when_utc, lat, lon)
    zl = solar_zenith(when_utc - timedelta(hours=1.5), lat, lon)
    d = max(0.0, math.cos(math.radians(z))) ** 0.75
    f2 = max(0.0, math.cos(math.radians(zl))) ** 0.6 if zl < 90 else 0.45 * math.exp(-(zl - 90) / 28)
    doy = when_utc.timetuple().tm_yday
    winter = math.cos(2 * math.pi * (doy - 15) / 365) * (1 if lat >= 0 else -1)   # +1 mid-January (north)
    return {"zenith": z, "d": d, "f2": max(f2, 0.0), "day_x": 1 + 0.15 * winter, "night_x": 1 - 0.25 * winter,
            "sun_up": z < 90}


def takeoff(hop_km: float, h: float = H_F2) -> float:
    phi = hop_km / (2 * R)
    if phi < 1e-6:
        return 90.0
    return math.degrees(math.atan((math.cos(phi) - R / (R + h)) / math.sin(phi)))


def hops(km: float) -> tuple[int, float]:
    n = 1
    while takeoff(km / n) < MIN_TAKEOFF:
        n += 1
    return n, takeoff(km / n)


def _sec_at(theta: float, h: float) -> float:
    s = R * math.cos(math.radians(theta)) / (R + h)
    return 1 / math.sqrt(max(1e-4, 1 - s * s))


def path(f: float, km: float, hour: float, sfi: float, n: int | None = None, sun: dict | None = None) -> dict:
    """The path over `n` hops (None: the fewest the geometry allows)."""
    if n is None:
        n, th = hops(max(20.0, km))
    else:
        th = takeoff(km / n)
    hop = km / n
    fo = fof2(hour, sfi, sun)
    muf = fo * _sec_at(th, H_F2)
    r = f / muf
    muf_db = 0.0 if r <= 0.9 else 3.0 if r <= 1.0 else 12.0 if r <= 1.1 else None
    dfac = sun["d"] if sun else _day(hour)
    absorb = n * _sec_at(th, H_D) * 300.0 * max(0.05, dfac) * (0.6 + 0.004 * sfi) / (f + 1.4) ** 2
    slant = n * 2 * math.hypot(hop / 2, H_F2)
    spread = 32.45 + 20 * math.log10(f) + 20 * math.log10(slant)
    loss = spread + absorb + 2.0 * (n - 1) + EXCESS_DB + (muf_db or 0.0)
    return {"hops": n, "takeoff": th, "fof2": fo, "muf": muf, "absorb": absorb, "loss": loss, "open": muf_db is not None}


def noise_dbm(f: float) -> float:
    """Band noise in an SSB bandwidth, a quiet-residential site: about S5 on 80 m, lower going up."""
    return -100.0 - 27.7 * math.log10(f / 3.9)                    # ITU-R P.372 residential slope


def _word(snr: float | None) -> str:
    return "unlikely" if snr is None or snr < 3 else "likely" if snr >= 12 else "maybe"


def chance_pct(snr: float | None) -> int:
    """A rough probability of copying the net on a given night (conditions vary ~±6 dB night to night)."""
    if snr is None:
        return 2
    return max(2, min(98, round(100 / (1 + math.exp(-(snr - 8) / 4)))))


def estimate(f: float, km: float | None, start: str, antenna: str, height_m: float, power_w: float,
             sfi: float = TYPICAL_SFI, good_km: tuple | None = None, when_utc=None, mid: tuple | None = None,
             ground: str = "average") -> dict:
    """{"chance": hear, "checkin": be heard, "pct_hear", "pct_send", "why", "band", "takeoff", "gain", "muf", "hops",
    "snr_hear", "snr_send"}. `good_km` = (from, to): where the operator hears stations well; it then defines the
    antenna's pattern instead of the model. The best of 1-6 hops is used (a long path may ride several short hops)."""
    if antenna in OLD_IDS:
        antenna, height_m = OLD_IDS[antenna]
    if antenna not in ANTENNAS:
        antenna = DEFAULT["antenna"]
    good = tuple(sorted(float(x) for x in good_km)) if good_km and all(good_km) else None
    hour = int(start[:2]) + int(start[3:5]) / 60 if start and start[:2].isdigit() else 19.0
    d = 400.0 if km is None else max(20.0, float(km))
    n_min, _ = hops(d)
    sun = sun_over(when_utc, mid[0], mid[1]) if when_utc is not None and mid else None
    g_of = (lambda th: measured_gain(good, th)) if good else (lambda th: gain(antenna, f, height_m, th, ground))
    dbm = lambda w: 10 * math.log10(max(0.1, w) * 1000)          # noqa: E731
    nz = noise_dbm(f)
    best = None
    for n in range(n_min, n_min + 6):
        p = path(f, d, hour, sfi, n, sun)
        if not p["open"]:
            if best is None:
                best = {"p": p, "hear": None, "send": None, "g": g_of(p["takeoff"])}
            continue
        th = p["takeoff"]
        g_me, g_ncs = g_of(th), gain(NCS["antenna"], f, NCS["height_m"], th)
        g_rx = g_me if good or g_me > -6 else -6 + (g_me + 6) * 0.3   # a weak antenna also hears less noise (a
        #                                                                 measured sweet spot already includes that)
        hear = dbm(NCS["power_w"]) + g_ncs + g_rx - p["loss"] - nz
        send = dbm(power_w) + g_me + g_ncs - p["loss"] - nz
        if best is None or best["hear"] is None or hear > best["hear"]:
            best = {"p": p, "hear": hear, "send": send, "g": g_me}
    p, hear, send, g_me = best["p"], best["hear"], best["send"], best["g"]
    th = p["takeoff"]
    name = ANTENNAS[antenna][0]
    hh = f"{int(hour):02d}:{int(round(hour % 1 * 60)):02d}"
    if sun:
        hh += (f" ({when_utc.strftime('%b %d')}, sun {90 - sun['zenith']:.0f}° up over the path)" if sun["sun_up"]
               else f" ({when_utc.strftime('%b %d')}, after dark over the path)")
    ant_words = (f"your antenna (you hear stations well at {good[0]:g}-{good[1]:g} km)" if good else
                 f"your {name.lower()}" + (f" at {height_m:g} m" if ANTENNAS[antenna][1] == "h" else ""))
    if hear is None:
        why = (f"{band_of(f)} at {hh}: the highest usable frequency for {round(d)} km is about {p['muf']:.1f} MHz, so "
               f"{f:.3f} MHz goes through the layer and skips over (solar flux {round(sfi)})")
    else:
        zone = ""
        if good and d < good[0]:
            zone = f" {round(d)} km is closer than your antenna's sweet spot: its signal goes up too steeply."
        elif good and d <= good[1] * p["hops"] and d >= good[0] * p["hops"]:
            zone = " In your antenna's sweet spot."
        why = (f"{band_of(f)} at {hh}, {round(d)} km: {p['hops']} hop{'s' if p['hops'] > 1 else ''} of "
               f"{round(d / p['hops'])} km, takeoff {th:.0f}°; highest usable frequency {p['muf']:.1f} MHz (solar flux "
               f"{round(sfi)}); daytime absorption {p['absorb']:.0f} dB. {ant_words[0].upper() + ant_words[1:]}: "
               f"{g_me:+.0f} dBi at {th:.0f}°.{zone} Signal over noise: hearing {hear:+.0f} dB, checking in with "
               f"{power_w:g} W {send:+.0f} dB")
    return {"chance": _word(hear), "checkin": _word(send), "pct_hear": chance_pct(hear), "pct_send": chance_pct(send),
            "why": why, "band": band_of(f), "takeoff": round(th), "gain": round(g_me, 1), "muf": round(p["muf"], 1),
            "hops": p["hops"], "km": round(d), "dark": (not sun["sun_up"]) if sun else None, "snr_hear": None if hear is None else round(hear),
            "snr_send": None if send is None else round(send)}


# ---------------------------------------------------------------- today's solar flux ------------------------------
SFI_URL = "https://services.swpc.noaa.gov/json/f107_cm_flux.json"


def solar_flux(cache: dict, fetch=None, max_age_s: float = 12 * 3600) -> tuple[float, str]:
    """(10.7 cm flux, where it came from). `cache` is a dict kept by the caller ({"sfi", "at"}); NOAA's public feed
    (no key, nothing about the station sent) is read at most twice a day; offline -> the last value or 130."""
    if cache.get("sfi") and time.time() - cache.get("at", 0) < max_age_s:
        return float(cache["sfi"]), "NOAA, today"
    try:
        rows = (fetch or _fetch)(SFI_URL)
        vals = [r for r in rows if isinstance(r, dict) and r.get("flux")]
        vals.sort(key=lambda r: str(r.get("time_tag") or ""))
        v = float(vals[-1]["flux"])
        if 50 <= v <= 400:
            cache.update(sfi=round(v), at=time.time())
            return float(round(v)), "NOAA, today"
    except Exception:  # noqa: BLE001 - offline: the model still runs
        pass
    if cache.get("sfi"):
        return float(cache["sfi"]), "NOAA, last known"
    return TYPICAL_SFI, "typical (offline)"


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HAM-Sidekick-Station"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read())
