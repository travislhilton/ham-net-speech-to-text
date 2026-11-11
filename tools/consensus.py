"""Re-run the net-wide callsign agreement and local-name fix-ups (station/consensus.py, station/fixups.py) for a
past day, or measure them on a saved transcript.

    python -m tools.consensus 2026-11-03                  # every net that day: report, write, re-index
    python -m tools.consensus 2026-11-03 --radio ftm --dry-run
    python -m tools.consensus --tx-json state.json --radio ftm --from 2026-11-03T19:56 --to 2026-11-03T20:17

A day re-run reads data/logs/<YYYYMMDD>.jsonl (the record), runs the agreement per net or conversation exactly as
the station does, appends an update line {"update": id, "consensus": ...} for each transmission whose record
changed (none when nothing changed: idempotent), and re-indexes data/history.db so attendance, minutes and the
ICS forms follow. Stop the station first or restart it after: a running station keeps its own copy of today.
--tx-json reads a list of transmissions (or /api/state's {"tx": [...]}) and only reports; it never writes.

The FCC copy is the station's (hamkit/fcc.py DEFAULT_DB, or --fcc); without it only what was heard in the net,
history and the roster count. Calls the FCC does not show Active are looked up on QRZ.com (the station's cache in
<data>/callbook, then the network when a login is stored; --qrz-offline: the cache only). The "distant" limit is
the station's Settings value (operator.json reach_km), else consensus.REACH_KM. The station's state comes from --state, station.yaml region_state, or the
operator's FCC licence."""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from hamkit import fcc as fccmod
from hamkit.callsigns import load_roster
from station import consensus, fixups, ics
from station.core import DATA, ROOT, load_config


def merged_log(path: Path) -> list[dict]:
    """A day log's transmissions with their update lines applied (the same rules as Station.load_today)."""
    txs, by_id = [], {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "update" in e:
            t = by_id.get(str(e["update"]))
            if not t:
                continue
            t.update({k: v for k, v in e.items() if k in ("accurate", "accurate_echo", "speaker_set", "net", "after",
                                                        "corrected", "consensus")})
            for k in ("net", "after", "corrected", "consensus"):
                if k in e and e[k] is None:
                    t.pop(k, None)
            if "accurate" in e:
                t.pop("accurate_echo", None)
            if "accurate_echo" in e:
                t.pop("accurate", None)
            if ("accurate" in e or "accurate_echo" in e) and "consensus" not in e:
                t.pop("consensus", None)
            continue
        if e.get("id") is not None:
            by_id[str(e["id"])] = e
            txs.append(e)
    return txs


def describe(rows: list[dict], recs: dict, res: dict) -> str:
    views = [ics.view(t, agreed=False) for t in rows]
    before, after = consensus.report(views), consensus.report(views, res)
    shown = {c for v in views for c in v.get("callsigns") or []}
    added = {c for calls in res["tx"].values() for c in calls} - shown
    lines = [f"  calls before: {before['distinct']} distinct, {before['once']} heard once;"
             f"  after: {after['distinct']} distinct, {after['once']} heard once"
             f" ({len(added)} found in the text that the transcript had not listed)"]
    moved = {h: e for h, e in res["calls"].items() if e["call"] != h}
    for h, e in sorted(moved.items()):
        where = "" if h in shown else "  [a live-text reading / a call in the text]"
        lines.append(f"  {h:>8} -> {e['call']:<8} conf {e['conf']:.2f}  {e['why']}{where}")
    unc = {}
    for h, e in res["calls"].items():
        if e.get("unconfirmed") and e["status"] != "distant" and (h in shown or e["call"] != h):
            unc[e["call"]] = e["unconfirmed"]
    if unc:
        lines.append(f"  counted, can't be confirmed ({len(unc)}):")
        lines += [f"    {c:>8}  {w}" for c, w in sorted(unc.items())]
    byq = sorted({e["call"] for e in res["calls"].values() if e.get("confirmed") == "QRZ.com"})
    if byq:
        lines.append(f"  confirmed by QRZ.com: {', '.join(byq)}")
    far = sorted((e["km"], h) for h, e in res["calls"].items() if e["status"] == "distant")
    if far:
        lines.append(f"  not counted, beyond VHF/UHF reach and nothing backs them ({len(far)}): "
                     + ", ".join(f"{h} {km} km" for km, h in far))
    amb = {h: e["candidates"] for h, e in res["calls"].items() if e.get("candidates") and e["call"] == h}
    for h, c in sorted(amb.items()):
        lines.append(f"  {h:>8} ?  could be {' / '.join(c)} (no clear winner: left as heard)")
    fx = sum(1 for r in recs.values() if r.get("fixups"))
    lines.append(f"  text fix-ups in {fx} transmission{'s' if fx != 1 else ''}; {len(recs)} records")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD: re-run that day's log")
    ap.add_argument("--radio")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--tx-json", help="measure a saved list of transmissions (report only)")
    ap.add_argument("--from", dest="frm"); ap.add_argument("--to")
    ap.add_argument("--data", default=str(DATA))
    ap.add_argument("--config", default=str(ROOT / "config"))
    ap.add_argument("--fcc", default=str(fccmod.DEFAULT_DB))
    ap.add_argument("--state", help="the station's state (two letters)")
    ap.add_argument("--net-name", action="append", default=[], help="a spoken net name to repair (repeatable)")
    ap.add_argument("--qrz-offline", action="store_true", help="QRZ.com answers from the cache only")
    ap.add_argument("--no-qrz", action="store_true", help="no QRZ.com answers at all")
    a = ap.parse_args(argv)
    if not a.date and not a.tx_json:
        ap.error("give a date or --tx-json")

    cfg = load_config(Path(a.config) / "station.yaml")
    me = (cfg.get("callsign") or "").upper()
    roster = load_roster(Path(a.config)) | ({me} - {""})
    con = sqlite3.connect(f"file:{a.fcc}?mode=ro", uri=True) if Path(a.fcc).exists() else None
    from hamkit.zipgeo import ZipGeo
    lic = consensus.Licences(con, geo=ZipGeo(Path(a.fcc).parent / "zipgeo.sqlite")) if con else None
    state = (a.state or cfg.get("region_state") or "").upper() or None
    if not state and lic and me:
        state = (lic.info([me]).get(me) or {}).get("state") or None
    fx = fixups.load(Path(a.config), Path(a.data))
    try:
        opr = json.loads((Path(a.data) / "operator.json").read_text(encoding="utf-8")).get("operator") or {}
    except (OSError, ValueError):
        opr = {}
    grid, reach = opr.get("grid"), opr.get("reach_km")
    qrz = None
    if not a.no_qrz:
        from hamkit import qrz as qrzmod
        qrz = qrzmod.Client(cache=Path(a.data) / "callbook" / "qrz_cache.sqlite")
        if a.qrz_offline:
            qrz.creds = None
    where = consensus.where_of(cfg, grid, lic, me)
    print(f"FCC: {'yes' if lic else 'NO (only the net, history and roster count)'}; state: {state or '?'}; "
          f"VHF/UHF distances: {'yes' if where and lic and lic.geo else 'no (python -m hamkit.zipgeo update)'}")

    data = Path(a.data)
    if a.tx_json:
        raw = json.loads(Path(a.tx_json).read_text(encoding="utf-8"))
        txs = raw.get("tx", []) if isinstance(raw, dict) else raw
        write = False
        log = None
    else:
        day = datetime.strptime(a.date, "%Y-%m-%d")
        log = data / "logs" / f"{day:%Y%m%d}.jsonl"
        if not log.exists():
            print(f"no log for {a.date}: {log}", file=sys.stderr)
            return 1
        txs = merged_log(log)
        write = not a.dry_run
    hist = None
    if (data / "history.db").exists() and not a.tx_json:
        from station.history import History
        hist = History(data, nets=lambda: cfg.get("schedule") or [], me=me)

    radios = sorted({t.get("radio") for t in txs if t.get("radio")})
    lines_out = []
    for rid in radios:
        if a.radio and rid != a.radio:
            continue
        sessions = ics.sessions(txs, rid)
        if a.frm or a.to:                                  # one span given by hand ("--to 20:17" holds 20:17:59)
            sessions = [{"start": a.frm or "0000", "end": a.to or "9999"}]
        for s in sessions:
            rows = [t for t in txs if t.get("radio") == rid and t.get("status") == "final"
                    and s["start"] <= t["start"] and t["start"][:len(s["end"])] <= s["end"]]
            if not rows:
                continue
            net = next(((t.get("net") or {}) for t in rows if (t.get("net") or {}).get("id")), {})
            entry = next((n for n in cfg.get("schedule") or [] if n.get("id") == net.get("id")), None)
            prior = {}
            if hist and net.get("id"):
                prior = hist.prior_checkins(net["id"], rows[0]["start"], exclude=hist.instance_of(rid, rows[0]["start"]))
            names = fixups.net_names_for(entry, a.net_name)
            views = [ics.view(t, agreed=False) for t in rows]
            heard = {c for v in views for c in v.get("callsigns") or []} | \
                    {c for t in rows for c in (t.get("callsigns") or [])}
            answers = consensus.qrz_answers(heard, qrz, lic, allow_network=bool(qrz and qrz.creds), budget=200)
            recs, res = consensus.for_session(rows, lic, prior, state, roster, me, fx, names, where=where,
                                              qrz=answers, reach_km=reach, net_name=(entry or {}).get("name"))
            print(f"{rid} {rows[0]['start']} .. {rows[-1]['start']}  {net.get('name') or ''}  ({len(rows)} transmissions)")
            print(describe(rows, recs, res))
            for t in rows:
                rec = recs.get(t["id"])
                if not t.get("corrected") and t.get("consensus") != rec:
                    lines_out.append({"update": t["id"], "consensus": rec})
    if write and lines_out:
        with log.open("a", encoding="utf-8") as f:
            for e in lines_out:
                f.write(json.dumps(e) + "\n")
        if hist:
            hist.catch_up(data / "logs")             # reads what was added since its last look, these lines too
    print(f"{len(lines_out)} transmission record{'s' if len(lines_out) != 1 else ''} "
          + ("written" if write else "would change (nothing written)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
