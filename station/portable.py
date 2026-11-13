"""Share settings between stations (Settings -> Share settings): export what another station can use, import it with a
preview and a choice of radios.

Owner 2026-11-13: set a second station up like the first by exporting its settings and importing them, as an operator would.

What travels (schema hamsidekick-settings/1, one JSON file):
* the net schedule, each net with the radio it used named by a HINT (the source radio's name, band and mode), because a
  radio's id means nothing on another PC;
* Sidekick's choices (license class, models, voice, speed) and, only when the exporter ticks "include my Sidekick
  identity", its on-air name, check-in line, operator name, station and region;
* the local names (the area pack).
What never travels: API keys, the call sign, radios and their sound inputs / PTT / CAT, history, recordings, which
radios Sidekick is on, and whether Sidekick is switched on.

Importing never deletes: a net with the same name and start time is updated (it keeps its id, its clash settings and, when
the import names no radio for it, its radio), other nets are added, nets only here stay. The current files are copied to
data/settings_backups/<time>/ first. Everything is checked with the same code as the editors (schedule.validate,
agent.settings, localnames.unpack): a bad item is reported and skipped, never half-applied.
"""
from __future__ import annotations

import copy
import shutil
import uuid
from datetime import datetime

from . import schedule as sched

SCHEMA = "hamsidekick-settings/1"
SIDEKICK_PLAIN = ("license_class", "model", "deep_model", "compare", "voice", "speed")
SIDEKICK_IDENTITY = ("name", "checkin", "operator", "station", "region")
NET_KEYS = ("name", "freq_mhz", "mode", "start", "duration_min", "ncs", "repeat", "exceptions", "alert_min",
            "emergency", "offset_mhz", "tone_hz", "reach_km", "checkins")


def _range(mhz) -> str:
    try:
        f = float(mhz)
    except (TypeError, ValueError):
        return ""
    return "HF" if f < 30 else "VHF" if f < 300 else "UHF"


def export(st, identity: bool = False) -> dict:
    radios = [{"key": r["id"], "name": r.get("name"), "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode"),
               "range": _range(r.get("freq_mhz"))} for r in st.radios.values()]
    nets = []
    for n in st.cfg.get("schedule") or []:
        o = {k: copy.deepcopy(n[k]) for k in NET_KEYS if n.get(k) is not None}
        if n.get("radio"):
            o["radio"] = n["radio"]
        for e in (o.get("exceptions") or {}).values():
            if isinstance(e, dict):
                e.pop("radio", None)
        nets.append(o)
    cfg = st.agent.cfg
    keys = SIDEKICK_PLAIN + (SIDEKICK_IDENTITY if identity else ())
    side = {k: cfg[k] for k in keys if k in cfg and cfg[k] not in (None, "")}
    d = st.local_names()
    from . import localnames
    return {"schema": SCHEMA, "exported": datetime.now().isoformat(timespec="seconds"), "identity_included": identity,
            "radios": radios, "nets": nets, "sidekick": side,
            "local_names": localnames.pack(d["names"], d["area"])}


def _check(doc) -> dict:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"not a settings file from HAM Sidekick Station (expected {SCHEMA})")
    return doc


def _suggest(src: dict, targets: list[dict]) -> str | None:
    """The radio here that fits a source radio: the same name, else the only one of the same band and mode."""
    for t in targets:
        if str(t.get("name") or "").casefold() == str(src.get("name") or "").casefold() and src.get("name"):
            return t["id"]
    fit = [t for t in targets if _range(t.get("freq_mhz")) == src.get("range") and t.get("mode") == src.get("mode")]
    return fit[0]["id"] if len(fit) == 1 else None


def _net_body(n: dict, rid) -> dict:
    b = {k: copy.deepcopy(n[k]) for k in NET_KEYS if k in n}
    b["radio"] = rid
    for e in (b.get("exceptions") or {}).values():
        if isinstance(e, dict):
            e.pop("radio", None)
    return b


def _plan(st, doc: dict, radio_map: dict | None) -> dict:
    """What an import would do, without doing it."""
    doc = _check(doc)
    targets = [{"id": r["id"], "name": r.get("name"), "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode")}
               for r in st.radios.values()]
    radio_map = {str(k): (str(v) if v else None) for k, v in (radio_map or {}).items()}
    have = {(str(n.get("name")).casefold(), n.get("start")): n for n in st.cfg.get("schedule") or []}
    src_radios = {r["key"]: r for r in doc.get("radios") or [] if isinstance(r, dict) and r.get("key")}
    used: dict[str, int] = {}
    add, upd, same, bad = [], [], [], []
    for n in doc.get("nets") or []:
        key = n.get("radio")
        if key:
            used[key] = used.get(key, 0) + 1
        rid = radio_map[key] if key in radio_map else (_suggest(src_radios.get(key, {}), targets) if key else None)
        try:
            body = _net_body(n, rid)
            v = sched.validate(body, st.radios)
        except ValueError as e:
            bad.append({"name": n.get("name"), "why": str(e)})
            continue
        cur = have.get((str(v["name"]).casefold(), v["start"]))
        if not cur:
            add.append((v, rid))
        else:
            keep = {k: cur.get(k) for k in v if k not in ("radio",)}
            if all(keep.get(k) == v.get(k) for k in v if k != "radio") and (rid is None or cur.get("radio") == rid):
                same.append(v)
            else:
                upd.append((cur, v, rid))
    side = doc.get("sidekick") if isinstance(doc.get("sidekick"), dict) else {}
    cfg = st.agent.cfg
    changes = [{"field": k, "from": cfg.get(k), "to": v} for k, v in side.items()
               if k in SIDEKICK_PLAIN + SIDEKICK_IDENTITY and cfg.get(k) != v]
    from . import localnames as ln
    ln_add = ln_upd = 0
    try:
        names, note = ln.unpack(doc.get("local_names"), ln.load(st.data)) if doc.get("local_names") else ([], {})
        ln_add, ln_upd = note.get("added", 0), note.get("updated", 0)
    except ValueError as e:
        bad.append({"name": "local names", "why": str(e)})
    return {"add": add, "update": upd, "same": same, "bad": bad, "radios": [
        {"key": k, "name": r.get("name"), "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode"), "nets": used.get(k, 0),
         "suggest": _suggest(r, targets), "chosen": radio_map.get(k, _suggest(r, targets))}
        for k, r in src_radios.items()], "targets": targets, "sidekick": changes,
        "local_names": {"added": ln_add, "updated": ln_upd}, "identity": bool(doc.get("identity_included"))}


def preview(st, doc, radio_map=None) -> dict:
    p = _plan(st, doc, radio_map)
    return {"nets": {"add": len(p["add"]), "update": len(p["update"]), "unchanged": len(p["same"]),
                     "invalid": p["bad"], "total": len(p["add"]) + len(p["update"]) + len(p["same"]) + len(p["bad"]),
                     "names_add": [v["name"] for v, _ in p["add"]][:60], "names_update": [c["name"] for c, _, _ in p["update"]][:60]},
            "radios": p["radios"], "targets": p["targets"], "sidekick": p["sidekick"],
            "local_names": p["local_names"], "identity": p["identity"]}


def apply(st, doc, radio_map=None) -> dict:
    p = _plan(st, doc, radio_map)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = st.data / "settings_backups" / stamp
    for f in ("schedule.json", "agent.json", "local_names.json"):
        if (st.data / f).exists():
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(st.data / f, bdir / f)
    nets_out = None
    with st.nets.lock:
        nets = [dict(n) for n in st.cfg.get("schedule") or []]
        ids = {n["id"] for n in nets}
        for cur, v, rid in p["update"]:
            i = next(i for i, n in enumerate(nets) if n["id"] == cur["id"])
            new = {"id": cur["id"]} | v
            if rid is None and cur.get("radio"):
                new["radio"] = cur["radio"]                       # the import names no radio here: keep this one
            if cur.get("beats"):
                new["beats"] = cur["beats"]
            nets[i] = new
        for v, _ in p["add"]:
            nid = "n" + uuid.uuid4().hex[:6]
            while nid in ids:
                nid = "n" + uuid.uuid4().hex[:6]
            ids.add(nid)
            nets.append({"id": nid} | v)
        if p["add"] or p["update"]:
            st.nets._commit(nets)
    side = doc.get("sidekick") if isinstance(doc.get("sidekick"), dict) else {}
    body = {c["field"]: c["to"] for c in p["sidekick"]}
    if body:
        st.agent.settings(body)
    ln_note = None
    if doc.get("local_names"):
        ln_note = st.local_names("import", {"pack": doc["local_names"]}).get("imported")
    return {"added": len(p["add"]), "updated": len(p["update"]), "unchanged": len(p["same"]),
            "invalid": p["bad"], "sidekick_fields": sorted(body), "local_names": ln_note,
            "backup": str(bdir) if bdir.exists() else None}
