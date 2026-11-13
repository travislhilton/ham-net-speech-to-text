"""File a stretch of a day's transmissions under the nets the schedule gives them now (each on its own frequency).

    python -m tools.refile_nets 20261111 --radio ftm150 --from 19:45 --to 21:30            (shows what would move)
    python -m tools.refile_nets 20261111 --radio ftm150 --from 19:45 --to 21:30 --apply    (station stopped first)

For transmissions filed under the wrong net (2026-11-11: two nets on one radio, an hour's net on another frequency
filed under the club net). Each final transmission in the window is given station/schedule.occurrence_at(...) for its
start and frequency, with nothing counted as ended; the net closes are left alone. --apply appends one
{"update": id, "net": ...} line per change to the day log (the station and its history index apply them as they do a
net close's re-tag) — stop the station first (schtasks /end /tn "HAM Sidekick Station") and start it after, so its memory
reloads the log.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from station import schedule as sched
from station.core import DATA


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("day", help="YYYYMMDD")
    ap.add_argument("--radio", required=True)
    ap.add_argument("--from", dest="frm", default="00:00")
    ap.add_argument("--to", default="23:59")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    doc = json.loads((DATA / "schedule.json").read_text(encoding="utf-8"))
    nets = doc.get("nets", doc) if isinstance(doc, dict) else doc
    log = DATA / "logs" / f"{a.day}.jsonl"
    rows, cur = {}, {}
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "update" in e:
            if "net" in e and str(e["update"]) in cur:
                cur[str(e["update"])] = e["net"]
            continue
        if e.get("radio") == a.radio and e.get("status") == "final" and a.frm <= str(e.get("start", ""))[11:16] <= a.to:
            rows[str(e["id"])] = e
            cur[str(e["id"])] = e.get("net")
    changes = []
    for tid, t in rows.items():
        o = sched.occurrence_at(nets, a.radio, datetime.fromisoformat(t["start"]), freq_mhz=t.get("freq_mhz"))
        new = {"key": o["key"], "id": o["id"], "date": o["date"], "name": o["name"]} if o else None
        old = cur.get(tid)
        if (old or {}).get("key") != (new or {}).get("key"):
            changes.append((t, old, new))
    for t, old, new in changes:
        print(f"{t['start'][11:19]} {t.get('freq_mhz')}  {(old or {}).get('name')} -> {(new or {}).get('name')}  "
              f"{(t.get('text') or '')[:50]}")
    print(f"{len(rows)} transmissions, {len(changes)} to move" + ("" if a.apply else " (dry run: --apply writes them)"))
    if a.apply and changes:
        with log.open("a", encoding="utf-8") as f:
            for t, _, new in changes:
                f.write(json.dumps({"update": t["id"], "net": new, "refiled": datetime.now().isoformat(timespec="seconds")})
                        + "\n")
        print(f"written to {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
