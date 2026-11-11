"""Replay a day's real traffic through Sidekick (the station agent) and report what it would have said.

    python -m tools.sidekick_replay 20261110 --radio net --from 19:30 --to 20:30 [--model rules] [--deep]
    python -m tools.sidekick_replay --script docs/sidekick/rehearsal.json --deep     (a made-up net: the two speeds)

Runs on a station PC (it reads data/logs/<day>.jsonl, the station's agent settings, local names and xAI key).
Nothing is transmitted and the live station is not touched: drafts go to data/sidekick_replay/<day>-<radio>.jsonl,
the agent's memory lives in a temporary file. Each final transmission is drafted in order with the clock set to the
moment it ended; a draft the live agent would have dropped (the next transmission began inside the 1.2 s quiet) is
skipped, as live. --model rules = the offline rules only (no network). --deep lets a "working on it" call the
thinking model (otherwise the question is noted and no answer is made).

The report: how often it spoke and of what kind, every draft that would have been sent (with the transmission it
followed), every draft the FCC checks stopped, and the model's latency.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from station import agent as agentmod
from station.core import DATA, ROOT, load_config


class _Bus:
    def publish(self, *a, **k):
        pass


class ReplayStation:
    """What the agent reads from a station, fed from a day log."""

    def __init__(self, data: Path, me: str, radios: dict):
        self.data, self.root, self.me = data, ROOT, me
        self.radios = radios
        self.tx: list[dict] = []
        self.now = datetime.now()
        self.bus = _Bus()
        self.cfg = {"schedule": []}

    def clock(self):
        return self.now

    def _current_by_radio(self, at):
        return {}                                           # the net comes from each transmission (as filed)


def load_day(path: Path) -> list[dict]:
    txs, by_id = [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "update" in e or e.get("id") is None:
            continue
        if str(e["id"]) in by_id:
            by_id[str(e["id"])].update(e)
        else:
            by_id[str(e["id"])] = e
            txs.append(e)
    return [t for t in txs if t.get("status") == "final"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("day", nargs="?", default=None, help="YYYYMMDD (or --script)")
    ap.add_argument("--radio", default="net")
    ap.add_argument("--script", type=Path, default=None,
                    help="a made-up net (JSON: net, freq_mhz, rows [{t, who, text}]) instead of a day log")
    ap.add_argument("--from", dest="frm", default="00:00")
    ap.add_argument("--to", default="23:59")
    ap.add_argument("--model", default=None, help="a model id, or 'rules' for the offline rules only")
    ap.add_argument("--deep", action="store_true", help="let 'working on it' call the thinking model")
    ap.add_argument("--max-calls", type=int, default=400)
    ap.add_argument("--quiet", action="store_true", help="only the summary")
    ap.add_argument("--data", type=Path, default=DATA, help="a data folder (default: this station's)")
    ap.add_argument("--call", default=None, help="the station's call (default: config/station.yaml)")
    a = ap.parse_args(argv)

    try:
        cfg = load_config()
    except (OSError, ValueError, KeyError):
        cfg = {}
    data = a.data
    if a.script:
        doc = json.loads(a.script.read_text(encoding="utf-8"))
        a.day = a.day or datetime.now().strftime("%Y%m%d")
        day = f"{a.day[:4]}-{a.day[4:6]}-{a.day[6:]}"
        net = {"key": "rehearsal@" + day, "id": "rehearsal", "name": doc.get("net") or "Rehearsal Net"}
        rows = []
        for i, r in enumerate(doc["rows"]):
            words = len(r["text"].split())
            rows.append({"id": f"r{i}", "radio": a.radio, "status": "final", "start": f"{day}T{r['t']}",
                         "duration_s": round(words * 0.4, 1), "text": r["text"], "speaker": r.get("who"),
                         "callsigns": [w.strip(".,?") for w in r["text"].split() if re.fullmatch(r"[AKNW][A-Z]?\d[A-Z]{1,3}[.,?]?", w)],
                         "freq_mhz": doc.get("freq_mhz"), "net": net})
    else:
        if not a.day:
            ap.error("a day (YYYYMMDD) or --script")
        rows = [t for t in load_day(data / "logs" / f"{a.day}.jsonl") if t.get("radio") == a.radio]
    lo, hi = a.frm.zfill(5), a.to.zfill(5)
    rows = [t for t in rows if lo <= str(t.get("start", ""))[11:16] <= hi]
    if not rows:
        print("no final transmissions in that window")
        return 1
    rcfg = next((r for r in cfg.get("radios", []) if r.get("id") == a.radio), {})
    radio = {"id": a.radio, "freq_mhz": rows[-1].get("freq_mhz") or rcfg.get("freq_mhz"),
             "mode": rcfg.get("mode") or ("FM" if (rows[-1].get("freq_mhz") or 146) > 30 else "LSB"), "carrier": False}
    st = ReplayStation(data, (a.call or cfg.get("callsign") or "N0CALL").upper(), {a.radio: radio})
    out_dir = data / "sidekick_replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / f"{a.day}-{a.radio}.jsonl"
    log.write_text("", encoding="utf-8")
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    ag = agentmod.Agent(st, log_path=log, state_path=tmp, per_hour=a.max_calls)
    ag.cfg = dict(ag.cfg, enabled=True, radios={a.radio: "draft"})
    if a.model == "rules":
        ag.providers = {p: (k, _offline) for p, (k, _) in ag.providers.items()}
    elif a.model:
        ag.cfg["model"], ag.cfg["compare"] = a.model, []
    if not a.deep:
        ag._advance_orig = ag._advance

        def no_deep(rid, s, d, rws, now, ctx, contact, lines, user):
            if d.get("kind") == "working":
                s["pending"] = {"id": "replay", "state": "working", "question": d.get("question"),
                                "asker": "?", "at": now.isoformat(), "model": "(not run in replay)"}
                return
            return ag._advance_orig(rid, s, d, rws, now, ctx, contact, lines, user)
        ag._advance = no_deep
    ag._later = lambda *x, **k: None                       # no timers: the replay drives every draft itself

    drafts, t0 = [], time.monotonic()
    for i, t in enumerate(rows):
        start = datetime.fromisoformat(t["start"])
        end = start + timedelta(seconds=float(t.get("duration_s") or 0))
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        st.tx.append(t)
        if t.get("noise") or not (t.get("text") or "").strip():
            continue
        if nxt and datetime.fromisoformat(nxt["start"]) <= end + timedelta(seconds=agentmod.DEBOUNCE_S):
            continue                                        # live, the next transmission cancels this draft
        st.now = end + timedelta(seconds=agentmod.DEBOUNCE_S)
        d = ag.draft(a.radio, t.get("id"))
        th = getattr(ag, "_thinking", None)
        if a.deep and th and th.is_alive():                 # time passes on the net: let the thinking finish
            th.join(agentmod.DEEP_TIMEOUT_S + 5)
            p = ag.state.get(a.radio, {}).get("pending") or {}
            if not a.quiet:
                print(f"    thinking ({p.get('model')}, {p.get('took_s')}s): {p.get('state')}"
                      f"{': ' + p.get('answer', '') if p.get('answer') else ''}{' ' + p.get('error', '') if p.get('error') else ''}")
        if d:
            drafts.append(d)
            if d.get("speak") and not a.quiet:
                mark = "SEND" if d.get("ok") else "STOP"
                print(f"{d['at'][11:19]} {mark} {d['kind']:<11} {d.get('model')} {d.get('latency_s')}s")
                print(f"    after: {d['heard'][:160]}")
                print(f"    says:  {d['say'][:300]}")
                if not d.get("ok"):
                    print("    stopped: " + "; ".join(f"{c['rule']}: {c['note']}" for c in d["fcc"] if not c["ok"]))
    spoke = [d for d in drafts if d.get("speak")]
    lat = sorted(d.get("latency_s") or 0 for d in drafts if d.get("model") != "rules")
    print(f"\n{a.day} {a.radio} {lo}-{hi}: {len(rows)} transmissions, {len(drafts)} drafts in "
          f"{time.monotonic() - t0:.0f}s; spoke {len(spoke)} "
          f"({', '.join(f'{k} {v}' for k, v in Counter(d['kind'] for d in spoke).items()) or 'never'}); "
          f"would send {sum(1 for d in spoke if d.get('ok'))}, stopped {sum(1 for d in spoke if not d.get('ok'))}; "
          f"offline-rules drafts {sum(1 for d in drafts if d.get('model') == 'rules')}")
    if lat:
        print(f"model latency p50 {lat[len(lat) // 2]}s, p95 {lat[min(len(lat) - 1, int(len(lat) * .95))]}s, "
              f"max {lat[-1]}s")
    print(f"drafts: {log}")
    return 0


def _offline(*a, **k):
    import urllib.error
    raise urllib.error.URLError("replay: offline rules only")


if __name__ == "__main__":
    sys.exit(main())
