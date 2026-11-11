"""Grok speech-to-text on a past net's recordings, compared with this station's own text.

    python -m tools.grok_compare 20261110 --radio net --from 21:00 --to 22:00 --limit 60

Runs on a station PC (its recordings, its xAI key). Each chosen transmission's recording is sent now, one at a time
(never queued: offline, it stops); the answers are kept in data/grok_stt/<day>.jsonl (where the dashboard's
"Compare today" reads them for today) and the comparison is printed: word errors and calls found by each engine,
against the operator's corrections (human) or the accurate pass (large-v3).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from station import grokstt
from station.core import DATA, ROOT, load_config


class _Bus:
    def publish(self, *a, **k):
        pass


class Shim:
    def __init__(self, data, me, roster):
        self.data, self.root, self.me, self.roster = data, ROOT, me, roster
        self.callbook, self.bus, self.tx, self.radios = None, _Bus(), [], {}

    def clock(self):
        return datetime.now()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("day")
    ap.add_argument("--radio", default=None)
    ap.add_argument("--from", dest="frm", default="00:00")
    ap.add_argument("--to", default="23:59")
    ap.add_argument("--limit", type=int, default=40, help="at most this many recordings sent")
    ap.add_argument("--report-only", action="store_true", help="send nothing, compare what is kept")
    a = ap.parse_args(argv)
    try:
        cfg = load_config()
    except (OSError, ValueError, KeyError):
        cfg = {}
    try:
        from hamkit.callsigns import load_roster
        roster = set(load_roster())
    except Exception:  # noqa: BLE001
        roster = set()
    me = (cfg.get("callsign") or "").upper()
    shim = Shim(DATA, me, roster | ({me} if me else set()))
    try:
        from station.callbook import Callbook
        shim.callbook = Callbook(roster=shim.roster)          # FCC database + country prefixes, as live
    except Exception:  # noqa: BLE001 - without it, written calls count only when on the roster
        pass
    g = grokstt.GrokSTT(shim)
    rows = [t for t in grokstt.day_rows(DATA / "logs" / f"{a.day}.jsonl") if t.get("status") == "final"
            and (not a.radio or t.get("radio") == a.radio) and a.frm <= str(t.get("start", ""))[11:16] <= a.to]
    done = {str(r["id"]) for r in g.results(a.day) if not r.get("error")}
    todo = [t for t in rows if t.get("wav") and not t.get("noise") and str(t["id"]) not in done
            and 1.0 <= float(t.get("duration_s") or 0) <= grokstt.MAX_CLIP_S
            and (DATA / t["wav"]).exists()][:0 if a.report_only else a.limit]
    sent = 0
    for t in todo:
        r = g.run(dict(t))
        if r.get("error"):
            print(f"{t['start'][11:19]} error: {r['error']}")
            if not g.online.should_try():
                print("xAI unreachable: stopped (nothing queued)")
                break
            continue
        sent += 1
        print(f"{t['start'][11:19]} {r['took_s']:>5}s  station: {(t.get('text') or '')[:90]}")
        print(f"{'':>15}Grok:    {r['text'][:90]}")
    c = g.compare(a.day, rows=rows)
    print(f"\nsent {sent}; compared {len(c['items'])} ({c['note']})")
    for k, v in c["summary"].items():
        pc = lambda x: "-" if x is None else f"{x * 100:.1f}%"   # noqa: E731
        print(f"  vs {k:<9} n={v['n']:<4} words={v['words']:<5} WER station {pc(v['live_wer'])}  Grok "
              f"{pc(v['grok_wer'])}   calls {v['calls']}: station {pc(v['live_call_recall'])}  Grok "
              f"{pc(v['grok_call_recall'])}   Grok time p50 {v['grok_s_p50']}s p95 {v['grok_s_p95']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
