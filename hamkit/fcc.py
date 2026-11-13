"""FCC ULS amateur license database: download, load into SQLite, keep current, look up callsigns.

Weekly full file (complete/l_amat.zip, ~200 MB, refreshed Sundays) + daily deltas
(daily/l_am_<day>.zip). Records are pipe-delimited; each license is one unique_system_identifier
(USI) across HD (header: status, dates), EN (entity: name, address, email), AM (amateur: class).
A callsign can have several USIs over the years (old cancelled licenses); lookups prefer the
Active one, then the latest grant.

    python -m hamkit.fcc update [--db PATH]     # weekly + any newer dailies; no-op when current
    python -m hamkit.fcc lookup W9XYZ [--db PATH]
"""
import argparse, io, json, os, sqlite3, sys, time, zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = "https://data.fcc.gov/download/pub/uls"
WEEKLY = f"{BASE}/complete/l_amat.zip"
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DEFAULT_DB = Path(os.environ.get("CALLBOOK_DIR", Path.home() / "data" / "callbook")) / "fcc_uls.sqlite"
CLASSES = {"A": "Advanced", "E": "Amateur Extra", "G": "General", "N": "Novice", "P": "Technician Plus",
           "T": "Technician"}
STATUS = {"A": "Active", "E": "Expired", "C": "Cancelled", "T": "Terminated", "L": "Pending legal status"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS hd (usi INTEGER PRIMARY KEY, call TEXT, status TEXT, grant_date TEXT,
                               expire_date TEXT, cancel_date TEXT);
CREATE TABLE IF NOT EXISTS en (usi INTEGER PRIMARY KEY, call TEXT, entity_name TEXT, first TEXT, mi TEXT,
                               last TEXT, suffix TEXT, email TEXT, street TEXT, city TEXT, state TEXT,
                               zip TEXT, po_box TEXT, frn TEXT, applicant_type TEXT);
CREATE TABLE IF NOT EXISTS am (usi INTEGER PRIMARY KEY, call TEXT, op_class TEXT, previous_call TEXT,
                               trustee_call TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS hd_call ON hd(call);
"""


def _date(mdy: str) -> str | None:
    """ULS dates are MM/DD/YYYY; store ISO so they sort."""
    if not mdy:
        return None
    m, d, y = mdy.split("/")
    return f"{y}-{m}-{d}"


def _records(raw: bytes, kind: str):
    """Yield field lists. A record can contain an embedded newline, so a line that does not start
    with the record type continues the previous one."""
    prev = None
    for line in raw.decode("latin-1").splitlines():
        if line.startswith(kind + "|"):
            if prev is not None:
                yield prev.split("|")
            prev = line
        elif prev is not None:
            prev += " " + line
    if prev is not None:
        yield prev.split("|")


def _f(r, i):
    return r[i].strip() if i < len(r) and r[i].strip() else None


def load_zip(con: sqlite3.Connection, zbytes: bytes) -> dict:
    """Upsert HD/EN/AM from one ULS zip (weekly or daily). Read in memory; nothing is extracted."""
    counts = {}
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        names = {n.upper(): n for n in z.namelist()}
        for kind in ("HD", "EN", "AM"):
            n = names.get(f"{kind}.DAT")
            if not n:
                continue
            raw = z.read(n)
            if kind == "HD":
                rows = [(int(r[1]), _f(r, 4), _f(r, 5), _date(_f(r, 7) or ""), _date(_f(r, 8) or ""),
                         _date(_f(r, 9) or "")) for r in _records(raw, "HD") if r[1].isdigit()]
                con.executemany("INSERT OR REPLACE INTO hd VALUES (?,?,?,?,?,?)", rows)
            elif kind == "EN":
                rows = [(int(r[1]), _f(r, 4), _f(r, 7), _f(r, 8), _f(r, 9), _f(r, 10), _f(r, 11), _f(r, 14),
                         _f(r, 15), _f(r, 16), _f(r, 17), _f(r, 18), _f(r, 19), _f(r, 22), _f(r, 23))
                        for r in _records(raw, "EN") if r[1].isdigit()]
                con.executemany("INSERT OR REPLACE INTO en VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            else:
                rows = [(int(r[1]), _f(r, 4), _f(r, 5), _f(r, 15), _f(r, 8)) for r in _records(raw, "AM")
                        if r[1].isdigit()]
                con.executemany("INSERT OR REPLACE INTO am VALUES (?,?,?,?,?)", rows)
            counts[kind] = len(rows)
    con.commit()
    return counts


def connect(db: Path = DEFAULT_DB) -> sqlite3.Connection:
    db = Path(db); db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    return con


def base_call(call: str) -> str:
    """K7ABC/P, K7ABC/M, VE3/K7ABC -> K7ABC (the US license part)."""
    parts = [p for p in call.upper().strip().split("/") if p]
    if not parts:
        return ""
    cands = [p for p in parts if any(c.isdigit() for c in p) and any(c.isalpha() for c in p) and len(p) >= 3]
    return max(cands, key=len) if cands else parts[0]


def lookup(con: sqlite3.Connection, call: str) -> dict | None:
    c = base_call(call)
    row = con.execute("""
        SELECT hd.usi, hd.call, hd.status, hd.grant_date, hd.expire_date, en.entity_name, en.first, en.mi,
               en.last, en.suffix, en.email, en.street, en.city, en.state, en.zip, en.po_box, en.frn,
               am.op_class, am.previous_call, am.trustee_call, en.applicant_type
        FROM hd LEFT JOIN en USING (usi) LEFT JOIN am USING (usi)
        WHERE hd.call = ?
        ORDER BY (hd.status = 'A') DESC, hd.grant_date DESC LIMIT 1""", (c,)).fetchone()
    if not row:
        return None
    k = ["usi", "call", "status", "grant_date", "expire_date", "entity_name", "first", "mi", "last", "suffix",
         "email", "street", "city", "state", "zip", "po_box", "frn", "op_class", "previous_call",
         "trustee_call", "applicant_type"]
    d = dict(zip(k, row))
    d["status_name"] = STATUS.get(d["status"], d["status"])
    d["class_name"] = CLASSES.get(d["op_class"] or "", None)
    d["is_club"] = d["applicant_type"] == "B" or bool(d["trustee_call"])
    d["name"] = " ".join(x for x in (d["first"], d["mi"], d["last"], d["suffix"]) if x) or d["entity_name"]
    d["valid"] = d["status"] == "A" and (d["expire_date"] or "9999") >= datetime.now().strftime("%Y-%m-%d")
    d["source"] = "FCC ULS"
    return d


def _meta(con, k, v=None):
    if v is None:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v)); con.commit()


UA = "HAM-Sidekick-Station"


def _curl(*args) -> bytes:
    """Downloads go through curl (built into macOS and Windows 11). The FCC's server answers curl
    but returned 403 to Python's urllib from the same host (2026-11-02); curl identifies itself
    honestly with this app's name."""
    import shutil, subprocess
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        raise RuntimeError("curl not found")
    r = subprocess.run([exe, "-sSfL", "--max-time", "900", "-A", UA, *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"curl {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _head(url) -> datetime | None:
    for line in _curl("-I", url).decode("latin-1").splitlines():
        if line.lower().startswith("last-modified:"):
            return parsedate_to_datetime(line.split(":", 1)[1].strip())
    return None


def _get(url) -> bytes:
    return _curl(url)


def update(db: Path = DEFAULT_DB, log=print) -> dict:
    """Bring the database current. Rebuilds from the weekly file when it is newer than the one
    loaded, then applies every daily file newer than what is loaded, oldest first."""
    con = connect(db)
    out = {"weekly": None, "daily": []}
    wk = _head(WEEKLY)
    have = _meta(con, "weekly_last_modified")
    if wk and (not have or wk > datetime.fromisoformat(have)):
        log(f"weekly {wk:%Y-%m-%d}: downloading")
        data = _get(WEEKLY)
        tmp = Path(str(db) + ".building")
        tmp.unlink(missing_ok=True)
        new = connect(tmp)
        out["weekly"] = load_zip(new, data)
        _meta(new, "weekly_last_modified", wk.isoformat())
        _meta(new, "daily_applied", wk.isoformat())
        new.close(); con.close()
        os.replace(tmp, db)                       # readers never see a half-built file
        con = connect(db)
        log(f"weekly loaded: {out['weekly']}")
    applied = datetime.fromisoformat(_meta(con, "daily_applied") or "1970-01-01T00:00:00+00:00")
    dailies = []
    for d in DAYS:
        url = f"{BASE}/daily/l_am_{d}.zip"
        try:
            lm = _head(url)
        except Exception as e:  # noqa: BLE001 — a missing daily is normal
            log(f"daily {d}: {e}"); continue
        if lm and lm > applied:
            dailies.append((lm, d, url))
    for lm, d, url in sorted(dailies):
        c = load_zip(con, _get(url)); out["daily"].append({d: c})
        _meta(con, "daily_applied", lm.isoformat())
        log(f"daily {d} {lm:%Y-%m-%d}: {c}")
    _meta(con, "checked_at", datetime.now(timezone.utc).isoformat())
    out["licenses"] = con.execute("SELECT count(*) FROM hd").fetchone()[0]
    con.close()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hamkit.fcc")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update")
    lk = sub.add_parser("lookup"); lk.add_argument("calls", nargs="+")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "update":
        t0 = time.time(); r = update(Path(a.db)); r["seconds"] = round(time.time() - t0, 1)
        print(json.dumps(r))
    elif a.cmd == "status":
        con = connect(Path(a.db))
        print(json.dumps({k: v for k, v in con.execute("SELECT k, v FROM meta")} |
                         {"licenses": con.execute("SELECT count(*) FROM hd").fetchone()[0]}))
    else:
        con = connect(Path(a.db))
        for c in a.calls:
            print(json.dumps(lookup(con, c)))


if __name__ == "__main__":
    sys.exit(main())
