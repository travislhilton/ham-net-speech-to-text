"""Keep recordings and net history for 6 weeks, then delete them (owner 2026-11-12: "We should only keep recordings
and net histories around for 6 weeks. Ensure that they are regularly cleaned up so we don't bloat the disk.").

A station records every transmission (about 130 MB a day on a busy two-radio station, measured 2026-11-12) and
nothing deleted it. The station sweeps 10 minutes after it starts and every 6 hours after that (never while a radio
is receiving or transcribing); `python -m station.retention [--dry-run]` sweeps by hand.

Older than the operator's choice (Settings → Keep recordings and net history, 1-104 weeks; else station.yaml
`keep_weeks`; default 6. Whole days, so a net is never cut in half). Shortening it sweeps a minute later:
  data/recordings/<YYYYMMDD>/      the audio of each transmission
  data/logs/<YYYYMMDD>.jsonl       the day logs (the record the history index is built from)
  data/history.db                  nets, transmissions, who took part, minutes and items of those days (then VACUUM)
  data/voice/*.npy                 voiceprints of those transmissions (Who? suggestions)
  data/agent_drafts.jsonl          the station agent's drafts
  data/grok_stt/, data/tts/, data/sidekick_replay/   comparisons and voice samples
Also, whatever their age:
  data/raw/<radio>/                a radio's last-hour ring is kept by the radio itself; files over a day old are
                                   from a radio since removed or renamed
  data/ov-cache/                   OpenVINO's compiled models: every re-measure adds ~1.8 GB. Over 3 GB, the least
                                   recently used files over 2 days old go (one in use is compiled again at the next start)
  data/station.log                 cut to its last 2 MB at start when over 10 MB (station/__main__.py)
Kept: settings, radios, the schedule, local names, learned fixes, keys, the callbook, net_heard.json and
netclose.json (both bounded already).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

KEEP_WEEKS = 6
MAX_WEEKS = 104
FIRST_S, EVERY_S, RETRY_S = 600, 6 * 3600, 600
RAW_STALE_S = 86400
OV_CAP = 3 * 1024 ** 3
OV_MIN_AGE_S = 2 * 86400
LOG_MAX, LOG_KEEP = 10 * 1024 ** 2, 2 * 1024 ** 2
DAY = re.compile(r"^(\d{8})(?:\.jsonl)?$")


def cutoff(keep_weeks: float = KEEP_WEEKS, today: date | None = None) -> date:
    """The first day kept: today and the 6 weeks before it."""
    return (today or date.today()) - timedelta(days=round(float(keep_weeks) * 7))


def _day(name: str) -> date | None:
    m = DAY.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _old_files(folder: Path, before_ts: float):
    if folder.is_dir():
        for f in folder.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < before_ts:
                    yield f
            except OSError:
                continue


def sweep(data: Path, keep_weeks: float = KEEP_WEEKS, history=None, agent=None, dry_run: bool = False,
          today: date | None = None, now: float | None = None) -> dict:
    """Delete what is older than keep_weeks. Returns {what: {"n": count, "bytes": freed}} and the first day kept."""
    data = Path(data)
    first = cutoff(keep_weeks, today)
    first_ts = datetime.combine(first, datetime.min.time()).timestamp()
    now = time.time() if now is None else now
    out: dict = {"keep_from": first.isoformat(), "dry_run": dry_run}

    def gone(what, p: Path):
        r = out.setdefault(what, {"n": 0, "bytes": 0})
        try:
            r["bytes"] += _size(p)
            if not dry_run:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            r["n"] += 1
        except PermissionError:                  # open (a compiled model the engine has loaded): next time
            r["bytes"] -= _size(p) if p.exists() else 0
            out["in use"] = out.get("in use", 0) + 1
        except OSError as e:
            out.setdefault("errors", []).append(f"{p.name}: {e}")

    rec = data / "recordings"
    if rec.is_dir():
        for d in sorted(rec.iterdir()):
            if d.is_dir() and (_day(d.name) or first) < first:
                gone("recordings", d)
    logs = data / "logs"
    old_logs = [f for f in sorted(logs.glob("*.jsonl")) if (_day(f.name) or first) < first] if logs.is_dir() else []
    for f in old_logs:
        gone("day logs", f)
    if history is not None:
        n = history.forget_before(first.isoformat(), [f.name for f in old_logs], dry_run=dry_run)
        if n:
            out["history"] = {"n": n, "bytes": 0}
    for f in _old_files(data / "voice", first_ts):
        gone("voiceprints", f)
    for sub in ("grok_stt", "tts", "sidekick_replay"):
        for f in _old_files(data / sub, first_ts):
            gone(sub, f)
    n = _drafts(data / "agent_drafts.jsonl", first, agent, dry_run)
    if n:
        out["agent drafts"] = {"n": n, "bytes": 0}
    for f in _old_files(data / "raw", now - RAW_STALE_S):
        gone("raw audio", f)
    if not dry_run and (data / "raw").is_dir():
        for d in (data / "raw").iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    _ov_cache(data / "ov-cache", now, gone)
    return out


def _drafts(p: Path, first: date, agent, dry_run: bool) -> int:
    """Rewrite the drafts log without lines from before `first` (under the agent's lock: it appends to it)."""
    if not p.exists():
        return 0
    lock = getattr(agent, "lock", None) or threading.Lock()
    with lock:
        keep, n = [], 0
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    at = str(json.loads(line).get("at") or "")
                except ValueError:
                    at = ""
                if at and at[:10] < first.isoformat():
                    n += 1
                else:
                    keep.append(line)
            if n and not dry_run:
                tmp = p.with_suffix(".tmp")
                tmp.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
                os.replace(tmp, p)
        except OSError:
            return 0
    if n and agent is not None and not dry_run and hasattr(agent, "drafts"):
        with lock:
            agent.drafts = [d for d in agent.drafts if str(d.get("at") or "")[:10] >= first.isoformat()]
    return n


def _ov_cache(folder: Path, now: float, gone):
    if not folder.is_dir():
        return
    files = []
    for f in folder.rglob("*"):
        try:
            if f.is_file():
                st = f.stat()
                files.append((max(st.st_mtime, st.st_atime), st.st_size, f))
        except OSError:
            continue
    total = sum(s for _, s, _ in files)
    for used, size, f in sorted(files, key=lambda x: x[0]):
        if total <= OV_CAP:
            break
        if now - used < OV_MIN_AGE_S:
            continue
        gone("OpenVINO cache", f)
        total -= size


def trim_log(p: Path, max_bytes: int = LOG_MAX, keep: int = LOG_KEEP) -> bool:
    """Cut a log file to its last `keep` bytes when it is over `max_bytes` (before the station opens it)."""
    try:
        if not p.exists() or p.stat().st_size <= max_bytes:
            return False
        with p.open("rb") as fh:
            fh.seek(-keep, os.SEEK_END)
            tail = fh.read()
        tail = tail[tail.find(b"\n") + 1:]
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(b"--- (older lines removed: log over %d MB)\n" % (max_bytes // 1024 ** 2) + tail)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def usage(data: Path) -> dict:
    """Disk used by what the sweep looks after (Settings → This PC)."""
    data = Path(data)
    out = {}
    for name, sub in (("recordings", "recordings"), ("day logs", "logs"), ("voiceprints", "voice"),
                      ("raw audio", "raw"), ("OpenVINO cache", "ov-cache")):
        p = data / sub
        out[name] = _size(p) if p.exists() else 0
    out["history"] = sum((data / f).stat().st_size for f in ("history.db", "history.db-wal")
                         if (data / f).exists())
    days = sorted(d for d in (_day(f.name) for f in (data / "logs").glob("*.jsonl")) if d) \
        if (data / "logs").is_dir() else []
    return {"bytes": out, "total": sum(out.values()), "oldest_day": days[0].isoformat() if days else None}


def say(res: dict) -> str:
    parts = [f"{k} {v['n']}" + (f" ({v['bytes'] / 1024 ** 2:.0f} MB)" if v.get("bytes") else "")
             for k, v in res.items() if isinstance(v, dict)]
    head = "would delete" if res.get("dry_run") else "deleted"
    return f"keeping from {res['keep_from']}: " + (f"{head} " + ", ".join(parts) if parts else "nothing older")


class Sweeper:
    """The station's sweep: 10 minutes after start, then every 6 hours, never while a radio is busy."""

    def __init__(self, station, keep_weeks=KEEP_WEEKS):
        self.st = station
        self.weeks = keep_weeks if callable(keep_weeks) else (lambda: keep_weeks)   # read at every sweep
        self.last: dict | None = None
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="retention").start()

    def soon(self):
        """Sweep in a minute (Settings: kept for less time)."""
        threading.Timer(60, self._wake.set).start()

    def _loop(self):
        self._wake.wait(FIRST_S)
        while True:
            self._wake.clear()
            busy = False
            try:
                busy = self.st.live_busy()
            except Exception:  # noqa: BLE001
                pass
            if busy:
                self._wake.wait(RETRY_S)
                continue
            self.run()
            self._wake.wait(EVERY_S)

    def run(self, dry_run: bool = False) -> dict:
        try:
            res = sweep(self.st.data, self.weeks(), history=getattr(self.st, "history", None),
                        agent=getattr(self.st, "agent", None), dry_run=dry_run)
            res["at"] = datetime.now().isoformat(timespec="seconds")
            if not dry_run:
                self.last = res
            if any(isinstance(v, dict) for v in res.values()) or res.get("errors"):
                print(f"{datetime.now():%H:%M:%S} retention: {say(res)}"
                      + (f"; errors: {res['errors'][:3]}" if res.get("errors") else ""), flush=True)
            return res
        except Exception as e:  # noqa: BLE001 — cleanup never stops the station
            print(f"retention: {type(e).__name__}: {e}", flush=True)
            return {"error": str(e)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent.parent / "data"))
    ap.add_argument("--keep-weeks", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true", help="say what would go; delete nothing")
    a = ap.parse_args(argv)
    keep = a.keep_weeks
    if keep is None:
        try:
            import yaml
            cfg = yaml.safe_load((Path(a.data).parent / "config" / "station.yaml").read_text(encoding="utf-8")) or {}
            keep = float(cfg.get("keep_weeks") or KEEP_WEEKS)
            op = json.loads((Path(a.data) / "operator.json").read_text(encoding="utf-8")).get("operator") or {}
            keep = float(op.get("keep_weeks") or keep)                 # Settings wins, as in the station
        except Exception:  # noqa: BLE001
            keep = KEEP_WEEKS
    hist = None
    if (Path(a.data) / "history.db").exists():
        from .history import History
        hist = History(Path(a.data))
    print(say(sweep(Path(a.data), keep, history=hist, dry_run=a.dry_run)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
