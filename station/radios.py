"""Radios… on the dashboard: add, replace or remove a radio, choosing its sound input from what is plugged in.

The starting list is station.yaml `radios:`. Once the operator saves the list on the dashboard it lives in
data/radios.json and station.yaml's list is no longer read (delete data/radios.json to start again from it), the same
rule as the net schedule. Settings the dashboard does not edit (meter bounds, segmenting) are kept per radio id.
A change restarts the station (the installer's watchdog task starts it again; on Windows it is asked to at once).

Sound inputs are matched by name: `device` is the Windows name without the driver part in brackets ("Digirig Lite
NET-REC"). An exact match wins over a partial one, so "Digirig Lite" never opens "Digirig Lite NET-REC" (a laptop
station had both: one for the net, one for Winlink, 2026-11-05). Renaming an input in Windows (Sound settings →
the input → Rename) keeps two identical adapters apart.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

EDITABLE = ("id", "name", "device", "gate", "mode", "freq_mhz")
GATES = ("carrier", "voice")
MODES = ("FM", "USB", "LSB", "AM", "CW", "DIG")
SKIP = ("stereo mix", "primary sound capture", "microsoft sound mapper")


def base_name(name: str) -> str:
    """'Digirig Lite NET-REC (USB Audio Device)' -> 'Digirig Lite NET-REC' (MME cuts names at 31 characters)."""
    return re.sub(r"\s*\(.*$", "", name or "").strip()


def pick_index(name: str, devices: list[dict]) -> int | None:
    """The input whose name matches `name`: exact base name first, then the full name, then a partial match."""
    want = (name or "").strip().lower()
    if not want:
        return None
    ins = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    for test in (lambda d: base_name(d["name"]).lower() == want, lambda d: d["name"].lower() == want,
                 lambda d: want in d["name"].lower()):
        for i, d in ins:
            if test(d):
                return i
    return None


def best_endpoint(name: str, friendly: list[str]) -> str | None:
    """The same rule for Windows endpoint names (the recording level: station/levels.py)."""
    want = (name or "").strip().lower()
    for test in (lambda f: base_name(f).lower() == want, lambda f: f.lower() == want, lambda f: want in f.lower()):
        for f in friendly:
            if test(f):
                return f
    return None


def inputs(devices: list[dict] | None = None, hostapis: list[dict] | None = None) -> list[str]:
    """The sound inputs plugged in, one per device (Windows lists each through several drivers), by base name."""
    if devices is None:
        try:
            import sounddevice as sd
            devices, hostapis = list(sd.query_devices()), list(sd.query_hostapis())
        except Exception:  # noqa: BLE001 — no audio stack (tests, a server): nothing to offer
            return []
    pref = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}
    seen: dict[str, int] = {}
    for d in devices:
        if d.get("max_input_channels", 0) <= 0:
            continue
        api = (hostapis or [{}] * 99)[d.get("hostapi", 0)].get("name", "") if hostapis else ""
        if api == "Windows WDM-KS":                      # kernel streaming: names like "Input ()", not usable here
            continue
        n = base_name(d["name"])
        if not n or n.lower().startswith(SKIP):
            continue
        rank = pref.get(api, 3)
        if n not in seen or rank < seen[n]:
            seen[n] = rank
    names = sorted(seen)                                  # a 31-character MME name is dropped when a longer one has it
    return [n for n in names if not any(o != n and o.startswith(n) and len(n) >= 31 for o in names)]


def fresh_inputs(timeout: float = 30) -> list[str] | None:
    """inputs() as Windows has them NOW. PortAudio (sounddevice) reads the device list once per process, so in the
    station an input renamed, re-plugged or added after it started stays invisible (a laptop station, 2026-11-07:
    the operator renamed a Digirig's input and the radio kept saying "not found"). A child process gets a fresh
    list. None when it could not be read."""
    import subprocess
    import sys
    code = "import json,sys; sys.path.insert(0, sys.argv[1]); from station.radios import inputs; print(json.dumps(inputs()))"
    try:
        p = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent.parent)],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def load(path: Path, base: list[dict]) -> list[dict]:
    """data/radios.json when present (kept settings from `base` by id), else station.yaml's list."""
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return base
    by_id = {r.get("id"): r for r in base}
    out = []
    for r in saved.get("radios", []):
        keep = {k: v for k, v in (by_id.get(r.get("id")) or {}).items() if k not in EDITABLE}
        out.append(keep | {k: r[k] for k in EDITABLE if r.get(k) not in (None, "")})
    return out


def clean(radios: list[dict]) -> list[dict]:
    """Check the dashboard's list; raises ValueError with words for the operator."""
    out, ids, devs = [], set(), set()
    for r in radios:
        name = str(r.get("name") or "").strip()[:60]
        if not name:
            raise ValueError("every radio needs a name")
        rid = str(r.get("id") or "").strip() or re.sub(r"\W+", "-", name).strip("-").lower()[:24] or "radio"
        if not re.fullmatch(r"[\w-]{1,24}", rid):
            raise ValueError(f"{name}: bad id")
        while rid in ids:
            rid += "-2"
        dev = str(r.get("device") or "").strip()
        if not dev:
            raise ValueError(f"{name}: choose its sound input")
        if dev.lower() in devs:
            raise ValueError(f"{name}: that sound input is already another radio's (each radio needs its own)")
        gate = r.get("gate") or "carrier"
        mode = str(r.get("mode") or "FM").upper()
        if gate not in GATES or mode not in MODES:
            raise ValueError(f"{name}: bad audio type or mode")
        f = r.get("freq_mhz")
        if f not in (None, ""):
            try:
                f = round(float(f), 4)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: frequency must be in MHz, like 146.940")
        ids.add(rid); devs.add(dev.lower())
        out.append({"id": rid, "name": name, "device": dev, "gate": gate, "mode": mode,
                    "freq_mhz": None if f in (None, "") else f})
    return out


def save(path: Path, radios: list[dict]):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"radios": radios}, indent=1), encoding="utf-8")
    tmp.replace(p)


CAPTURE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
NAME_PROP = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PORT_PROP = "{b3f8fa53-0004-438e-9003-51a46e139bfc},39"     # the physical (USB) device an input belongs to


def why_missing(name: str, endpoints: list[dict] | None = None) -> str:
    """When a named input is gone, say what Windows did with it. A driver update can retire a renamed input and make
    a new one on the same USB port with the default name (a laptop station, 2026-11-07: an Intel audio driver moved
    both Digirigs to its USB-audio offload path; both came back as "Microphone"). Windows lets only a person rename
    an input (Sound settings; the registry refuses even SYSTEM), so the station names the step.
    endpoints: [{"name", "port", "present"}] (read from the registry when None)."""
    if endpoints is None:
        endpoints = _capture_endpoints()
    if any(e["present"] and base_name(e["name"]) == base_name(name) for e in endpoints):
        return ""                                              # it is there now (the station looks again by itself)
    old = [e for e in endpoints if not e["present"] and e["port"] and base_name(e["name"]) == base_name(name)]
    for e in old:
        now = [x for x in endpoints if x["present"] and x["port"] == e["port"]]
        if now:
            return (f"Windows replaced the input '{name}' (a driver update?) with one named '{now[0]['name']}' on the "
                    f"same USB port. Rename that input to '{name}' in Settings > System > Sound > Input (the radio "
                    "comes back by itself), or choose its new input in Radios….")
    if old:
        return f"the input '{name}' is not plugged in"
    return ""


def _capture_endpoints() -> list[dict]:
    try:
        import winreg
    except ImportError:
        return []
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CAPTURE_KEY)
    except OSError:
        return []
    i = 0
    while True:
        try:
            guid = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, guid)
            state = winreg.QueryValueEx(k, "DeviceState")[0]
            p = winreg.OpenKey(k, "Properties")
            get = lambda v: (lambda r: r[0] if isinstance(r[0], str) else "")(winreg.QueryValueEx(p, v))
            out.append({"name": get(NAME_PROP), "port": _try(get, PORT_PROP), "present": state == 1})
        except OSError:
            continue
    return out


def _try(f, v):
    try:
        return f(v)
    except OSError:
        return ""
