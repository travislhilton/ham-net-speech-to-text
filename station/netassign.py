"""Nets without a radio here, given radios a group at a time (Nets -> "N nets have no radio on this station").

A net is kept on the schedule with no radio when an import found no radio for it (Share settings, station/portable.py), when
its radio was removed, or when it names a radio this PC does not have. Such nets are grouped by the radio they used at the
other station (the `from_radio` hint an import leaves: that radio's name) or, with no hint, by their own band and mode, and
each group gets a suggested radio: a radio of the same name, else the radio already recording most nets of the group's band
and mode, else the only radio here tuned to that band and mode. Assigning never deletes and never touches a net that has a radio; the schedule is
copied to data/settings_backups/<time>/ first, and every net is checked again with schedule.validate.
"""
from __future__ import annotations

import shutil
from datetime import datetime

from . import schedule as sched
from .portable import _range


def _targets(st) -> list[dict]:
    return [{"id": r["id"], "name": r.get("name"), "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode")}
            for r in st.radios.values() if r.get("role") != "winlink"]


def _waiting(st, n: dict) -> bool:
    return not n.get("radio") or n["radio"] not in st.radios


def _band(n: dict) -> tuple[str, str]:
    return _range(n.get("freq_mhz")) or "?", str(n.get("mode") or "").upper()


def _group_of(n: dict) -> tuple[str, str, str | None]:
    """(key, label, the other station's radio name or None)."""
    was = n.get("from_radio") if isinstance(n.get("from_radio"), dict) else {}
    if was.get("name"):
        return f"from:{str(was['name']).casefold()}", f"{was['name']} at the other station", str(was["name"])
    band, mode = _band(n)
    words = " ".join(x for x in (band if band != "?" else "", mode) if x) or "no frequency"
    return f"band:{band}|{mode}", f"{words} nets", None


def _suggest(st, name: str | None, members: list[dict], nets: list[dict], targets: list[dict]) -> str | None:
    """The same radio name here; else the radio already recording most nets of the group's band and mode; else the
    only radio here tuned to that band and mode."""
    if name:
        for t in targets:
            if str(t.get("name") or "").casefold() == name.casefold():
                return t["id"]
    kinds: dict[tuple, int] = {}
    for m in members:
        kinds[_band(m)] = kinds.get(_band(m), 0) + 1
    if not kinds:
        return None
    band, mode = max(sorted(kinds), key=lambda k: kinds[k])
    ids = {t["id"] for t in targets}
    have: dict[str, int] = {}
    for n in nets:
        if not _waiting(st, n) and n["radio"] in ids and _band(n) == (band, mode):
            have[n["radio"]] = have.get(n["radio"], 0) + 1
    if have:
        return max(sorted(have), key=lambda k: have[k])
    fit = [t for t in targets if _band(t) == (band, mode)]
    return fit[0]["id"] if len(fit) == 1 else None


def groups(st) -> dict:
    nets = list(st.cfg.get("schedule") or [])
    targets = _targets(st)
    out: dict[str, dict] = {}
    for n in nets:
        if not _waiting(st, n):
            continue
        key, label, name = _group_of(n)
        g = out.setdefault(key, {"key": key, "label": label, "from_radio": name, "nets": [], "_m": []})
        g["_m"].append(n)
        g["nets"].append({"id": n["id"], "name": n.get("name"), "start": n.get("start"),
                          "freq_mhz": n.get("freq_mhz"), "mode": n.get("mode"),
                          "was": n.get("radio") if n.get("radio") else None})
    for g in out.values():
        g["suggest"] = _suggest(st, g["from_radio"], g.pop("_m"), nets, targets)
        g["nets"].sort(key=lambda x: (str(x.get("start") or ""), str(x.get("name") or "")))
    gl = sorted(out.values(), key=lambda g: (-len(g["nets"]), g["label"]))
    return {"groups": gl, "waiting": sum(len(g["nets"]) for g in gl), "targets": targets}


def assign(st, choices: dict) -> dict:
    """choices = {group key: radio id or ""}. A group left empty stays without a radio."""
    choices = {str(k): str(v or "") for k, v in (choices or {}).items()}
    targets = {t["id"] for t in _targets(st)}
    bad = [v for v in choices.values() if v and v not in targets]
    if bad:
        raise ValueError(f"no radio called {bad[0]!r} here")
    done, failed = 0, []
    bdir = None
    with st.nets.lock:
        nets = [dict(n) for n in st.cfg.get("schedule") or []]
        for i, n in enumerate(nets):
            if not _waiting(st, n):
                continue
            rid = choices.get(_group_of(n)[0])
            if not rid:
                continue
            body = {k: v for k, v in n.items() if k not in ("id", "from_radio", "beats")} | {"radio": rid}
            try:
                v = sched.validate(body, st.radios)
            except ValueError as e:
                failed.append({"name": n.get("name"), "why": str(e)})
                continue
            new = {"id": n["id"]} | v
            if n.get("beats"):
                new["beats"] = n["beats"]
            nets[i] = new
            done += 1
        if done:
            src = st.data / "schedule.json"
            if src.exists():
                bdir = st.data / "settings_backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
                bdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, bdir / "schedule.json")
            st.nets._commit(nets)
    return {"assigned": done, "failed": failed, "backup": str(bdir) if bdir else None} | groups(st)
