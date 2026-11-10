"""Net history, search, analysis and minutes: a SQLite index beside the day logs (data/history.db).

Why SQLite: it is in Python's standard library (nothing to install on the Windows station), one file, crash-safe
(WAL), fast at 100k+ transmissions, and its FTS5 full-text index makes "search everything" instant offline.
The day logs (data/logs/<date>.jsonl) stay the record: this index can be deleted and is rebuilt from them on the next
start. Only what the operator types here (minutes edits, item status, notes) lives nowhere else, so it is never
overwritten by re-extraction or the accurate pass.

    instance   one occurrence of a scheduled net ("<net id>@<occurrence date>"), or a conversation outside any
               net ("<radio>@<first transmission>"): start/end, frequency, net control, counts, accurate-pass status,
               Winlink messages sent, and when and by whom it was closed (station/netclose.py: net control's
               closing words, or the operator's End net)
    tx         every final transmission ever (live + accurate text, speaker, callsigns, recording path) — a pointer
               into the day log and data/recordings, never a copy of the audio
    part       who took part in an instance: ncs | checkin (spoke, or was acknowledged by net control) | mentioned
    item       the minutes: announcement / event / action / decision / traffic / urgent; `auto` = drafted by
               station/minutes.py, `edited` = the operator changed it (re-extraction then leaves it alone)
    minutes    per instance: the operator's notes, net control and attendance corrections

Taking part (attendance, the regulars): net control, the speaker of a transmission (station/speaker.py), a station
net control acknowledges ("K7ABC, thank you, you're in the log"), or one that starts a transmission with its own
call and is not being spoken to ("W9XYZ, water is rising on Mill Creek Road"). A callsign only mentioned is not.

Which instance a transmission belongs to: the scheduled occurrence on its radio from 15 minutes before it starts
to 15 minutes after it ends (schedule.occurrence_at); otherwise the latest net or conversation on that radio if
it is under 15 quiet minutes old on the same frequency (a net running over stays that net); otherwise a new
conversation.
"""
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from hamkit.hints import Hinter
from . import ics, minutes, recur, speaker
from .schedule import occurrence_at

GAP = timedelta(minutes=15)
REGULAR_LAST, REGULAR_SHARE = 8, 0.5
REGULAR_RULE = (f"A regular took part in at least half of the last {REGULAR_LAST} times the net met "
                f"(and at least twice). Taking part = spoke, checked in or was acknowledged by net control; "
                f"being mentioned does not count.")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS inst(key TEXT PRIMARY KEY, kind TEXT, net_id TEXT, occ_date TEXT, name TEXT, radio TEXT,
  start TEXT, end TEXT, sched_start TEXT, sched_end TEXT, freq_mhz REAL, mode TEXT, ncs TEXT, net_type TEXT,
  emergency INTEGER DEFAULT 0, tx_count INTEGER DEFAULT 0, stations INTEGER DEFAULT 0, acc_final INTEGER DEFAULT 0,
  acc_total INTEGER DEFAULT 0, winlink TEXT, dirty INTEGER DEFAULT 1, closed_at TEXT, closed_by TEXT, closed_tx TEXT);
CREATE INDEX IF NOT EXISTS inst_net ON inst(net_id, occ_date);
CREATE INDEX IF NOT EXISTS inst_radio ON inst(radio, end);
CREATE INDEX IF NOT EXISTS inst_start ON inst(start);
CREATE TABLE IF NOT EXISTS tx(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, radio TEXT, start TEXT,
  freq_mhz REAL, duration_s REAL, text TEXT, live_text TEXT, calls TEXT, speaker TEXT, speaker_how TEXT,
  noise INTEGER, stage TEXT, wav TEXT, inst TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS tx_inst ON tx(inst, start);
CREATE INDEX IF NOT EXISTS tx_start ON tx(start);
CREATE TABLE IF NOT EXISTS part(inst TEXT, call TEXT, role TEXT, first_at TEXT, n INTEGER, ord INTEGER,
  source TEXT, PRIMARY KEY(inst, call));
CREATE INDEX IF NOT EXISTS part_call ON part(call);
CREATE TABLE IF NOT EXISTS item(id INTEGER PRIMARY KEY, inst TEXT, net_id TEXT, kind TEXT, text TEXT, who TEXT,
  by TEXT, place TEXT, people TEXT, category TEXT, event_at TEXT, due TEXT, until TEXT, status TEXT DEFAULT 'open',
  tx_id TEXT, tx_start TEXT, auto INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, fp TEXT, created TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS item_inst ON item(inst);
CREATE INDEX IF NOT EXISTS item_net ON item(net_id, kind, status);
CREATE TABLE IF NOT EXISTS minutes(inst TEXT PRIMARY KEY, notes TEXT, ncs TEXT, added TEXT, removed TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS station(call TEXT PRIMARY KEY, name TEXT, city TEXT, state TEXT);
"""
ITEM_FIELDS = ("kind", "text", "who", "place", "event_at", "due", "until", "status", "category")
KINDS = ("announcement", "event", "action", "decision", "traffic", "urgent", "note")
STATUSES = ("open", "done", "handled", "dismissed")


def has_fts5(con) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._probe USING fts5(x)")
        con.execute("DROP TABLE temp._probe")
        return True
    except sqlite3.OperationalError:
        return False


def fts_query(q: str) -> str:
    """User words -> an FTS5 query: every word must match (as a prefix), "quoted words" as a phrase."""
    parts = []
    for m in re.finditer(r'"([^"]+)"|([A-Za-z0-9]+)', q or ""):
        if m.group(1):
            w = re.findall(r"[A-Za-z0-9]+", m.group(1))
            if w:
                parts.append('"' + " ".join(w) + '"')
        else:
            parts.append(f'"{m.group(2)}"*')
    return " ".join(parts)


def _words(q: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", q or "")]


def mark(text: str, q: str, width=150) -> str:
    """A snippet of `text` around the first query word, the words wrapped in \\x02 ... \\x03 (the FTS-less path)."""
    text = text or ""
    ws = _words(q)
    pos = min([i for i in (text.lower().find(w) for w in ws) if i >= 0] or [0])
    a = max(0, pos - width // 3)
    snip = ("…" if a else "") + text[a:a + width] + ("…" if a + width < len(text) else "")
    for w in sorted(ws, key=len, reverse=True):
        snip = re.sub(rf"(?i)(?<![\x02\w])({re.escape(w)}\w*)", "\x02\\1\x03", snip)
    return snip


class History:
    def __init__(self, data_dir: Path, nets=lambda: [], radios=lambda: {}, callbook=None, clock=datetime.now,
                 me: str = "", bus=None):
        self.path = Path(data_dir) / "history.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.nets, self.radios, self.callbook, self.clock, self.me, self.bus = nets, radios, callbook, clock, me, bus
        self.lock = threading.RLock()
        self.con = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMA)
        have = {r[1] for r in self.con.execute("PRAGMA table_info(inst)")}
        for col in ("closed_at", "closed_by", "closed_tx"):        # an index made before net closes were heard
            if col not in have:
                self.con.execute(f"ALTER TABLE inst ADD COLUMN {col} TEXT")
        if "corrected" not in {r[1] for r in self.con.execute("PRAGMA table_info(tx)")}:   # before tap-to-correct
            self.con.execute("ALTER TABLE tx ADD COLUMN corrected TEXT")
        self.con.execute("CREATE INDEX IF NOT EXISTS tx_corrected ON tx(corrected) WHERE corrected IS NOT NULL")
        self.fts = has_fts5(self.con)
        if self.fts:
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS tx_fts USING fts5(text, live, calls)")
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5(text, who, place)")
        self.status = {"indexing": False, "days": 0, "fts": self.fts}
        self._urgent_sig = None
        row = self.con.execute("SELECT v FROM meta WHERE k='tx_index'").fetchone()
        if not row or row[0] != "2":
            self._reindex()

    def close(self):
        with self.lock:
            self.con.close()

    # ---------- small helpers ----------
    def _q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchone()

    def _meta(self, k, v=None):
        if v is None:
            r = self._one("SELECT v FROM meta WHERE k=?", (k,))
            return r["v"] if r else None
        self.con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, str(v)))

    def _net(self, nid: str) -> dict | None:
        return next((n for n in self.nets() or [] if n.get("id") == nid), None)

    # ---------- indexing ----------
    def _begin(self) -> bool:
        if self.con.in_transaction:
            return False
        self.con.execute("BEGIN")
        return True

    def ingest(self, entry: dict):
        """One line of a day log: a final transmission, or an update to one (accurate text, "Who?")."""
        with self.lock:
            own = self._begin()
            try:
                self._ingest(entry)
            finally:
                if own:
                    self.con.execute("COMMIT")

    def _ingest(self, e: dict):
        if "update" in e:
            row = self.con.execute("SELECT raw, inst FROM tx WHERE id=?", (str(e["update"]),)).fetchone()
            if not row:
                return
            raw = json.loads(row["raw"])
            raw.update({k: v for k, v in e.items() if k in ("accurate", "accurate_echo", "speaker_set", "net", "after",
                                                            "corrected", "consensus")})
            if "corrected" in e and e["corrected"] is None:        # Undo correction
                raw.pop("corrected", None)
            if "accurate" in e:
                raw.pop("accurate_echo", None)
            if "accurate_echo" in e:
                raw.pop("accurate", None)
            if ("accurate" in e or "accurate_echo" in e) and "consensus" not in e:
                raw.pop("consensus", None)                 # new text: the net-wide agreement is made again
            if "consensus" in e and not e["consensus"]:
                raw.pop("consensus", None)
            key = row["inst"]
            if "after" in e or "net" in e:                         # a net close undone: back into the net
                for k in ("net", "after"):
                    if raw.get(k) is None:
                        raw.pop(k, None)
                old = key
                key = self._assign(raw)
                if key != old:
                    self.con.execute("UPDATE inst SET dirty=1 WHERE key=?", (old,))
            self._put(raw, key)
            return
        if e.get("status") != "final" or not e.get("id") or not e.get("start") or not e.get("radio"):
            return
        old = self.con.execute("SELECT inst FROM tx WHERE id=?", (str(e["id"]),)).fetchone()
        self._put(e, old["inst"] if old else self._assign(e))

    @staticmethod
    def _searchable(raw: dict, v: dict) -> tuple[str, list]:
        """What search reads besides the shown text: the live draft and its callsigns only until the accurate pass
        has read the transmission. The live prompt spelled the operator's own call and wrote it into other people's
        transmissions (fixed 2026-11-06, 3e2a9af); the accurate text did not have it, but search still found the call
        in the live draft (owner 2026-11-10: "None of them say <my call>")."""
        if raw.get("accurate"):
            return "", list(dict.fromkeys(v.get("callsigns") or []))
        return raw.get("text") or "", list(dict.fromkeys(list(v.get("callsigns") or []) + list(raw.get("callsigns") or [])))

    def _reindex(self):
        """Once: rebuild the callsigns and the search index of every transmission under _searchable."""
        rows = self.con.execute("SELECT rowid, raw FROM tx").fetchall()
        for r in rows:
            try:
                raw = json.loads(r["raw"])
            except (TypeError, ValueError):
                continue
            v = ics.view(raw)
            live, calls = self._searchable(raw, v)
            self.con.execute("UPDATE tx SET calls=? WHERE rowid=?", (" " + " ".join(calls) + " ", r["rowid"]))
            if self.fts:
                self.con.execute("DELETE FROM tx_fts WHERE rowid=?", (r["rowid"],))
                self.con.execute("INSERT INTO tx_fts(rowid, text, live, calls) VALUES(?,?,?,?)",
                                 (r["rowid"], v.get("text") or "", live,
                                  " ".join(calls + ([v["speaker"]] if v.get("speaker") else []))))
        self.con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('tx_index', '2')")
        self.con.commit()

    def _put(self, raw: dict, key: str):
        v = ics.view(raw)
        acc = raw.get("accurate") or raw.get("accurate_echo")
        stage = (acc.get("stage") or "final") if acc else None
        live_idx, calls = self._searchable(raw, v)
        args = (str(raw["id"]), raw.get("radio"), raw["start"], raw.get("freq_mhz"), raw.get("duration_s"),
                v.get("text") or "", raw.get("text") or "", " " + " ".join(calls) + " ", v.get("speaker"),
                v.get("speaker_how"), 1 if v.get("noise") else 0, stage, raw.get("wav"), key, json.dumps(raw),
                (raw.get("corrected") or {}).get("at"))
        cur = self.con.execute(
            "INSERT INTO tx(id, radio, start, freq_mhz, duration_s, text, live_text, calls, speaker, speaker_how, noise,"
            " stage, wav, inst, raw, corrected) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " radio=excluded.radio, start=excluded.start, freq_mhz=excluded.freq_mhz, duration_s=excluded.duration_s,"
            " text=excluded.text, live_text=excluded.live_text, calls=excluded.calls, speaker=excluded.speaker,"
            " speaker_how=excluded.speaker_how, noise=excluded.noise, stage=excluded.stage, wav=excluded.wav,"
            " inst=excluded.inst, raw=excluded.raw, corrected=excluded.corrected RETURNING rowid", args)
        rowid = cur.fetchone()[0]
        if self.fts:
            self.con.execute("DELETE FROM tx_fts WHERE rowid=?", (rowid,))
            self.con.execute("INSERT INTO tx_fts(rowid, text, live, calls) VALUES(?,?,?,?)",
                             (rowid, v.get("text") or "", live_idx,
                              " ".join(calls + ([v["speaker"]] if v.get("speaker") else []))))
        t = raw["start"]
        self.con.execute("UPDATE inst SET start=MIN(COALESCE(start, ?), ?), end=MAX(COALESCE(end, ?), ?), dirty=1"
                         " WHERE key=?", (t, t, t, t, key))

    def _assign(self, raw: dict) -> str:
        rid, t = raw["radio"], raw["start"]
        at = datetime.fromisoformat(t)
        tag = raw.get("net") or {}
        after = raw.get("after")                  # heard after its net was closed: never that net again
        occ = None
        if tag.get("id") and tag.get("date"):
            occ = tag
        elif not after:
            occ = occurrence_at(self.nets() or [], rid, at, freq_mhz=raw.get("freq_mhz"))
        if occ:
            key = recur.key(occ["id"], occ["date"])
            if not self.con.execute("SELECT 1 FROM inst WHERE key=?", (key,)).fetchone():
                n = self._net(occ["id"]) or {}
                o = occ if occ.get("start") else (occurrence_at(self.nets() or [], rid, at) or {})
                self.con.execute(
                    "INSERT INTO inst(key, kind, net_id, occ_date, name, radio, sched_start, sched_end, freq_mhz,"
                    " mode, ncs, emergency) VALUES(?, 'net', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, occ["id"], occ["date"], occ.get("name") or n.get("name") or occ["id"], rid, o.get("start"),
                     o.get("end"), o.get("freq_mhz") or raw.get("freq_mhz"), o.get("mode") or n.get("mode"),
                     n.get("ncs"), 1 if n.get("emergency") else 0))
            return key
        last = self.con.execute("SELECT key, end, freq_mhz FROM inst WHERE radio=? AND start<=? ORDER BY end DESC"
                                " LIMIT 1", (rid, t)).fetchone()
        if last and last["end"] and at - datetime.fromisoformat(last["end"]) <= GAP and last["key"] != after \
                and (last["freq_mhz"] or None) == (raw.get("freq_mhz") or None):
            return last["key"]
        key = f"{rid}@{t}"
        self.con.execute("INSERT OR IGNORE INTO inst(key, kind, radio, start, end, freq_mhz) VALUES(?, 'conversation',"
                         " ?, ?, ?, ?)", (key, rid, t, t, raw.get("freq_mhz")))
        return key

    def catch_up(self, logs: Path) -> int:
        """Index what the day logs hold that the index does not (the first run: everything). Idempotent: each file's
        indexed length is kept, so a restart reads only what was added since."""
        logs = Path(logs)
        n = 0
        self.status.update(indexing=True)
        try:
            for f in sorted(logs.glob("*.jsonl")):
                size = f.stat().st_size
                off = int(self._meta(f"off:{f.name}") or 0)
                if off >= size:
                    continue
                with f.open("rb") as fh:
                    fh.seek(off if off <= size else 0)
                    data = fh.read()
                end = data.rfind(b"\n") + 1                   # a line still being written waits for the next time
                with self.lock:
                    self.con.execute("BEGIN")
                    try:
                        for line in data[:end].splitlines():
                            try:
                                self._ingest(json.loads(line))
                                n += 1
                            except (ValueError, KeyError, TypeError):
                                continue
                        self._meta(f"off:{f.name}", off + end)
                        self.con.execute("COMMIT")
                    except Exception:
                        self.con.execute("ROLLBACK")
                        raise
                self.status["days"] += 1
            self.flush(publish=False)
        finally:
            self.status.update(indexing=False)
        return n

    def mark_logged(self, log_file: Path):
        """The live station appended to today's log and indexed it already: a restart need not read it again."""
        try:
            with self.lock:
                self._meta(f"off:{Path(log_file).name}", Path(log_file).stat().st_size)
        except OSError:
            pass

    # ---------- derived: participants, counts, minutes drafts ----------
    def flush(self, keys=None, publish=True) -> list[str]:
        """Recompute the instances changed since last time (or `keys`)."""
        with self.lock:
            if keys is None:
                keys = [r["key"] for r in self.con.execute("SELECT key FROM inst WHERE dirty=1")]
            own = self._begin()
            try:
                for k in keys:
                    self._refresh(k)
            finally:
                if own:
                    self.con.execute("COMMIT")
        if publish and keys and self.bus:
            for k in keys:
                i = self._one("SELECT key, kind, net_id, name, radio, start, end FROM inst WHERE key=?", (k,))
                if i:
                    self.bus.publish("inst", dict(i))
            self._publish_urgent()
        return keys

    def _views(self, key: str) -> list[dict]:
        rows = self.con.execute("SELECT raw FROM tx WHERE inst=? ORDER BY start", (key,)).fetchall()
        return [ics.view(json.loads(r["raw"])) for r in rows]

    def _detect_ncs(self, views, sched_ncs):
        pat = [re.compile(r"(?:this is|de)\s+(?P<c>[A-Z0-9]{3,8})[,.]?\s+(?:\w+\s+){0,4}?net control", re.I),
               re.compile(r"net control(?: station)?(?: is| tonight is| this evening is| today is| for tonight is)?"
                          r"[,:]?\s+(?P<c>[A-Z0-9]{3,8})\b", re.I)]
        for v in views[:40]:
            for p in pat:
                m = p.search(v.get("text") or "")
                if m and m.group("c").upper() in (v.get("callsigns") or []):
                    return m.group("c").upper()
        if sched_ncs:
            return sched_ncs
        from .netclose import learn_ncs                   # the station that opened the net / took roll
        return learn_ncs(views)

    def _refresh(self, key: str):
        inst = self.con.execute("SELECT * FROM inst WHERE key=?", (key,)).fetchone()
        if not inst:
            return
        views = self._views(key)
        if not views:
            self.con.execute("UPDATE inst SET dirty=0, tx_count=0 WHERE key=?", (key,))
            return
        mins = self.con.execute("SELECT * FROM minutes WHERE inst=?", (key,)).fetchone()
        added = json.loads(mins["added"] or "[]") if mins else []
        removed = set(json.loads(mins["removed"] or "[]")) if mins else set()
        real = [v for v in views if not v.get("noise")]
        n = self._net(inst["net_id"]) if inst["net_id"] else None
        ncs = (mins["ncs"] if mins and mins["ncs"] else None) or self._detect_ncs(real, (n or {}).get("ncs")
                                                                                  or inst["ncs"])
        people: dict[str, dict] = {}

        def seen(c, role, at):
            p = people.setdefault(c, {"role": role, "first": at, "n": 0, "source": "auto"})
            p["n"] += 1
            if role == "checkin" and p["role"] == "mentioned":
                p["role"], p["first"] = "checkin", at
        for v in real:
            calls = list(v.get("callsigns") or [])
            if v.get("speaker"):
                seen(v["speaker"], "checkin", v["start"])
            text = v.get("text") or ""
            acked = speaker.addressed(text, calls) if speaker._ACK.search(text) else set()
            lead = re.match(r"\W*([A-Z0-9]{1,3}\d[A-Z0-9]{1,4})[,.]\s", text)      # "W9XYZ, water is rising on ...":
            if lead and lead.group(1) in calls and lead.group(1) not in speaker.addressed(text, calls) \
                    and not speaker._ACK.search(text):                       # a station leading with its own call
                acked = acked | {lead.group(1)}
            for c in calls:
                if c != v.get("speaker"):
                    seen(c, "checkin" if c in acked else "mentioned", v["start"])
        for c in added:
            people.setdefault(c, {"role": "checkin", "first": inst["start"], "n": 0, "source": "operator"})
            people[c]["role"], people[c]["source"] = "checkin", "operator"
        if ncs:
            people.setdefault(ncs, {"role": "ncs", "first": inst["start"], "n": 0, "source": "auto"})["role"] = "ncs"
        for c in removed:
            people.pop(c, None)
        mine = {c for c in (self.me,) if c}
        self.con.execute("DELETE FROM part WHERE inst=?", (key,))
        order = sorted((p["first"], c) for c, p in people.items() if p["role"] in ("checkin", "ncs"))
        ordmap = {c: i + 1 for i, (_, c) in enumerate(order)}
        for c, p in people.items():
            self.con.execute("INSERT INTO part VALUES(?,?,?,?,?,?,?)",
                             (key, c, p["role"], p["first"], p["n"], ordmap.get(c), "me" if c in mine else p["source"]))
            self._note_call(c)
        h = Hinter(self.me, window=10 ** 6)
        for v in real:
            h.recent.append(v.get("text") or "")
        nt = h.classify()["type"]
        emergency = bool((n or {}).get("emergency")) or bool(inst["emergency"]) or nt == "emergency"
        with_wav = [v for v in views if v.get("wav")]
        acc_final = sum(1 for v in with_wav if (v.get("accurate") or v.get("accurate_echo") or {}).get("stage", "final")
                        == "final" and (v.get("accurate") or v.get("accurate_echo")))
        attended = sum(1 for p in people.values() if p["role"] in ("checkin", "ncs"))
        self.con.execute("UPDATE inst SET start=?, end=?, tx_count=?, stations=?, ncs=?, net_type=?, emergency=?,"
                         " acc_final=?, acc_total=?, dirty=0, name=COALESCE(?, name) WHERE key=?",
                         (views[0]["start"], views[-1]["start"], len(real), attended, ncs, nt, 1 if emergency else 0,
                          acc_final, len(with_wav), (n or {}).get("name"), key))
        day = date.fromisoformat((inst["occ_date"] or views[0]["start"])[:10])
        drafts = minutes.refine(minutes.extract(real, day, emergency), real)
        self._merge_items(key, inst["net_id"], drafts)

    def _merge_items(self, key, net_id, drafts):
        """Auto items follow the transcript; an item the operator touched (edited=1) is never changed or re-added."""
        now = self.clock().isoformat(timespec="seconds")
        old = {r["fp"]: r for r in self.con.execute("SELECT * FROM item WHERE inst=? AND auto=1", (key,))}
        keep = set()
        for d in drafts:
            fp = d["fp"]
            keep.add(fp)
            vals = (d["kind"], d["text"], d.get("who"), d.get("by"), d.get("place"),
                    json.dumps(d.get("people") or []), d.get("category"), d.get("event_at"), d.get("due"),
                    d.get("until"), d.get("tx_id"), d.get("tx_start"))
            r = old.get(fp)
            if r and r["edited"]:
                continue
            if r:
                self.con.execute("UPDATE item SET kind=?, text=?, who=?, by=?, place=?, people=?, category=?,"
                                 " event_at=?, due=?, until=?, tx_id=?, tx_start=?, updated=? WHERE id=?",
                                 vals + (now, r["id"]))
                iid = r["id"]
            else:
                iid = self.con.execute(
                    "INSERT INTO item(kind, text, who, by, place, people, category, event_at, due, until, tx_id,"
                    " tx_start, inst, net_id, status, auto, edited, fp, created, updated)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',1,0,?,?,?)",
                    vals + (key, net_id, fp, now, now)).lastrowid
            self._fts_item(iid)
        for fp, r in old.items():
            if fp not in keep and not r["edited"]:
                self.con.execute("DELETE FROM item WHERE id=?", (r["id"],))
                if self.fts:
                    self.con.execute("DELETE FROM item_fts WHERE rowid=?", (r["id"],))

    def _fts_item(self, iid):
        if not self.fts:
            return
        r = self.con.execute("SELECT text, who, place FROM item WHERE id=?", (iid,)).fetchone()
        self.con.execute("DELETE FROM item_fts WHERE rowid=?", (iid,))
        if r:
            self.con.execute("INSERT INTO item_fts(rowid, text, who, place) VALUES(?,?,?,?)",
                             (iid, r["text"] or "", r["who"] or "", r["place"] or ""))

    def _note_call(self, call):
        if not self.callbook or self.con.execute("SELECT 1 FROM station WHERE call=?", (call,)).fetchone():
            return
        try:
            self.note_station(self.callbook.card(call))
        except Exception:  # noqa: BLE001 — a callbook problem never stops the index
            pass

    def note_station(self, card: dict | None):
        """Remember a callsign's name and place, so a search for a name finds the station."""
        if not card or not card.get("call"):
            return
        with self.lock:
            self.con.execute("INSERT INTO station(call, name, city, state) VALUES(?,?,?,?) ON CONFLICT(call) DO UPDATE"
                             " SET name=COALESCE(excluded.name, name), city=COALESCE(excluded.city, city),"
                             " state=COALESCE(excluded.state, state)",
                             (card["call"], card.get("name"), card.get("city"), card.get("state")))

    def note_winlink(self, radio: str, start: str, forms: list[str], to: list[str]):
        """Winlink messages were put in the Outbox for the net or conversation that holds `start`."""
        with self.lock:
            r = self.con.execute("SELECT inst FROM tx WHERE radio=? AND start>=? ORDER BY start LIMIT 1",
                                 (radio, start)).fetchone()
            if not r:
                return
            i = self.con.execute("SELECT winlink FROM inst WHERE key=?", (r["inst"],)).fetchone()
            w = json.loads(i["winlink"] or "[]") if i else []
            w.append({"at": self.clock().isoformat(timespec="seconds"), "forms": forms, "to": to})
            self.con.execute("UPDATE inst SET winlink=? WHERE key=?", (json.dumps(w), r["inst"]))

    def note_close(self, key: str, at: str, by: str | None, tx_id: str | None):
        """The net was closed at the transmission starting `at` (net control's words, or "operator": End net)."""
        with self.lock:
            self.con.execute("UPDATE inst SET closed_at=?, closed_by=?, closed_tx=?, dirty=1 WHERE key=?",
                             (at, by, tx_id, key))

    def undo_close(self, key: str):
        with self.lock:
            self.con.execute("UPDATE inst SET closed_at=NULL, closed_by=NULL, closed_tx=NULL, dirty=1 WHERE key=?",
                             (key,))

    def prior_checkins(self, net_id: str | None, before: str, exclude: str | None = None) -> dict[str, int]:
        """{call: how many earlier times of this scheduled net it took part in (net control or checked in)} — the
        net-wide callsign agreement's "has checked in here before" (station/consensus.py)."""
        if not net_id:
            return {}
        rows = self._q("SELECT part.call, COUNT(DISTINCT part.inst) n FROM part JOIN inst ON inst.key=part.inst"
                       " WHERE inst.net_id=? AND inst.start<? AND inst.key!=? AND part.role IN ('checkin','ncs')"
                       " GROUP BY part.call", (net_id, before, exclude or ""))
        return {r["call"]: r["n"] for r in rows}

    def instance_of(self, radio: str, start: str) -> str | None:
        r = self._one("SELECT inst FROM tx WHERE radio=? AND start>=? ORDER BY start LIMIT 1", (radio, start))
        return r["inst"] if r else None

    # ---------- reading ----------
    def _inst_dict(self, r) -> dict:
        d = dict(r)
        d.pop("dirty", None)
        d["winlink"] = json.loads(d.get("winlink") or "[]")
        d["emergency"] = bool(d.get("emergency"))
        return d

    def instances(self, net=None, radio=None, kind=None, limit=200, before=None) -> list[dict]:
        self.flush()
        w, a = ["tx_count>0"], []
        for col, val in (("net_id", net), ("radio", radio), ("kind", kind)):
            if val:
                w.append(f"{col}=?"); a.append(val)
        if before:
            w.append("start<?"); a.append(before)
        rows = self._q(f"SELECT * FROM inst WHERE {' AND '.join(w)} ORDER BY start DESC LIMIT ?", a + [int(limit)])
        out = []
        for r in rows:
            d = self._inst_dict(r)
            c = self._q("SELECT kind, COUNT(*) n FROM item WHERE inst=? AND status!='dismissed' GROUP BY kind",
                        (r["key"],))
            d["items"] = {x["kind"]: x["n"] for x in c}
            out.append(d)
        return out

    def _items(self, where, args) -> list[dict]:
        out = []
        for r in self._q(f"SELECT item.*, inst.name inst_name, inst.occ_date, inst.start inst_start, inst.radio radio"
                         f" FROM item LEFT JOIN inst ON inst.key=item.inst WHERE {where}"
                         f" ORDER BY COALESCE(item.tx_start, item.created)", args):
            d = dict(r)
            d["people"] = json.loads(d.get("people") or "[]")
            d["auto"], d["edited"] = bool(d["auto"]), bool(d["edited"])
            out.append(d)
        return out

    def instance(self, key: str) -> dict:
        self.flush([key])
        r = self._one("SELECT * FROM inst WHERE key=?", (key,))
        if not r:
            raise LookupError(key)
        d = self._inst_dict(r)
        with self.lock:
            d["tx"] = [{k: v.get(k) for k in ("id", "start", "text", "speaker", "speaker_how", "callsigns", "noise",
                                              "wav", "duration_s", "freq_mhz", "corrected")}
                       | {"live_text": v.get("live_text")}
                       for v in self._tx_views(key)]
        names = {s["call"]: dict(s) for s in self._q("SELECT * FROM station WHERE call IN (SELECT call FROM part"
                                                      " WHERE inst=?)", (key,))}
        d["participants"] = [dict(p) | {"name": (names.get(p["call"]) or {}).get("name"),
                                        "city": (names.get(p["call"]) or {}).get("city")}
                             for p in self._q("SELECT call, role, first_at, n, ord, source FROM part WHERE inst=?"
                                              " ORDER BY ord IS NULL, ord, call", (key,))]
        d["items"] = self._items("item.inst=? AND item.status!='dismissed'", (key,))
        d["dismissed"] = self._items("item.inst=? AND item.status='dismissed'", (key,))
        if d.get("net_id"):                                 # open items from earlier times this net met
            d["carried"] = self._items("item.net_id=? AND item.inst!=? AND inst.start<? AND item.status='open'"
                                       " AND item.kind IN ('action','urgent','traffic')",
                                       (d["net_id"], key, d["start"] or ""))
            prev = self._one("SELECT key FROM inst WHERE net_id=? AND start<? AND tx_count>0 ORDER BY start DESC"
                             " LIMIT 1", (d["net_id"], d["start"] or ""))
            nxt = self._one("SELECT key FROM inst WHERE net_id=? AND start>? AND tx_count>0 ORDER BY start LIMIT 1",
                            (d["net_id"], d["start"] or ""))
            d["prev"], d["next"] = (prev["key"] if prev else None), (nxt["key"] if nxt else None)
        else:
            d["carried"], d["prev"], d["next"] = [], None, None
        m = self._one("SELECT * FROM minutes WHERE inst=?", (key,))
        d["notes"] = m["notes"] if m else ""
        d["ncs_set"] = m["ncs"] if m else None
        return d

    def _tx_views(self, key):
        out = []
        for r in self.con.execute("SELECT raw FROM tx WHERE inst=? ORDER BY start", (key,)):
            raw = json.loads(r["raw"])
            v = ics.view(raw)
            v["live_text"] = raw.get("text")
            out.append(v)
        return out

    def tx_raw(self, tid: str) -> dict | None:
        """A transmission as the day log has it (all its updates merged), from any day."""
        r = self._one("SELECT raw FROM tx WHERE id=?", (str(tid),))
        return json.loads(r["raw"]) if r else None

    def corrected_rows(self) -> list[dict]:
        """Every transmission the operator corrected (tap-to-correct), oldest correction first, with its net's name:
        the ground truth station/corrections.py learns fix-ups from and measures accuracy against."""
        self.flush()
        rows = self._q("SELECT tx.raw, inst.name, inst.kind, inst.mode FROM tx LEFT JOIN inst ON inst.key=tx.inst"
                       " WHERE tx.corrected IS NOT NULL ORDER BY tx.corrected")
        out = []
        for r in rows:
            raw = json.loads(r["raw"])
            if raw.get("corrected"):
                raw["_net"] = r["name"] if r["kind"] == "net" else "Conversations"
                raw["_mode"] = r["mode"]
                out.append(raw)
        return out

    def tx_wav(self, tid: str) -> str | None:
        r = self._one("SELECT wav FROM tx WHERE id=?", (tid,))
        return r["wav"] if r else None

    # ---------- search ----------
    def search(self, q: str, net=None, radio=None, frm=None, to=None, call=None, limit=50, kind=None) -> dict:
        """Everything that matches, grouped: transmissions (live + accurate text, callsigns, speaker), stations
        (callsigns and callbook names), nets (scheduled names and past instances), minutes items."""
        q = (q or "").strip()
        call = (call or "").strip().upper() or None
        out = {"q": q, "fts": self.fts, "tx": [], "tx_total": 0, "stations": [], "nets": [], "items": []}
        if not q and not call:
            return out
        self.flush()
        w, a = [], []
        if net:
            w.append("inst.net_id=?"); a.append(net)
        if radio:
            w.append("tx.radio=?"); a.append(radio)
        if kind in ("net", "conversation"):                     # a transmission outside any net is a conversation
            w.append("COALESCE(inst.kind, 'conversation')=?"); a.append(kind)
        if frm:
            w.append("tx.start>=?"); a.append(frm[:10])
        if to:
            w.append("tx.start<?"); a.append((date.fromisoformat(to[:10]) + timedelta(days=1)).isoformat())
        if call:
            w.append("(tx.calls LIKE ? OR tx.speaker=?)"); a += [f"% {call} %", call]
        cols = ("tx.id, tx.radio, tx.start, tx.speaker, tx.speaker_how, tx.wav, tx.duration_s, tx.inst, inst.name"
                " net_name, inst.kind inst_kind, inst.net_id, tx.text, tx.live_text, tx.noise")
        with self.lock:
            if q and self.fts and fts_query(q):
                where = " AND ".join(["tx_fts MATCH ?"] + w)
                args = [fts_query(q)] + a
                base = f"FROM tx_fts JOIN tx ON tx.rowid=tx_fts.rowid LEFT JOIN inst ON inst.key=tx.inst WHERE {where}"
                rows = self.con.execute(f"SELECT {cols}, snippet(tx_fts, -1, char(2), char(3), '…', 18) snip {base}"
                                        f" ORDER BY tx.start DESC LIMIT ?", args + [int(limit)]).fetchall()
                total = self.con.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
            else:
                ww, aa = list(w), list(a)
                for t in _words(q):
                    ww.append("(tx.text LIKE ? OR (json_extract(tx.raw, '$.accurate') IS NULL AND tx.live_text LIKE ?)"
                              " OR tx.calls LIKE ?)")
                    aa += [f"%{t}%"] * 3
                where = " AND ".join(ww) or "1"
                base = f"FROM tx LEFT JOIN inst ON inst.key=tx.inst WHERE {where}"
                rows = self.con.execute(f"SELECT {cols}, NULL snip {base} ORDER BY tx.start DESC LIMIT ?",
                                        aa + [int(limit)]).fetchall()
                total = self.con.execute(f"SELECT COUNT(*) {base}", aa).fetchone()[0]
        for r in rows:
            d = dict(r)
            snip = d.pop("snip") or mark(d["text"] if not q or q.lower() in (d["text"] or "").lower()
                                         or not d["live_text"] else d["text"], q)
            if q and "\x02" not in snip and d.get("live_text"):
                snip = mark(d["live_text"], q)
            d["snippet"] = snip
            d.pop("live_text", None)
            out["tx"].append(d)
        out["tx_total"] = total
        words = _words(q)
        if words or call:
            pats = [w.upper() for w in words] if words else [call]
            st = []
            for p in pats[:3]:
                st += self._q("SELECT call, COUNT(*) times, MAX(first_at) last, MIN(first_at) first FROM part WHERE"
                              " call LIKE ? AND role!='mentioned' GROUP BY call ORDER BY times DESC LIMIT 20",
                              (p + "%",))
                st += self._q("SELECT s.call, (SELECT COUNT(*) FROM part p WHERE p.call=s.call AND p.role!="
                              "'mentioned') times, (SELECT MAX(first_at) FROM part p WHERE p.call=s.call) last, NULL"
                              " first FROM station s WHERE s.name LIKE ? OR s.city LIKE ? LIMIT 20",
                              (f"%{p}%", f"%{p}%"))
            seen = {}
            for s in st:
                if s["call"] not in seen:
                    seen[s["call"]] = dict(s)
            names = {r["call"]: dict(r) for r in self._q(
                f"SELECT * FROM station WHERE call IN ({','.join('?' * len(seen)) or 'NULL'})", list(seen))}
            out["stations"] = [s | {"name": (names.get(c) or {}).get("name"), "city": (names.get(c) or {}).get("city"),
                                    "state": (names.get(c) or {}).get("state")} for c, s in list(seen.items())[:20]]
        if words and kind != "conversation":
            for n in self.nets() or []:
                if all(w in n.get("name", "").lower() for w in words):
                    out["nets"].append({"net_id": n["id"], "name": n["name"], "summary": recur.summary(n),
                                        "scheduled": True})
            like = " AND ".join(["(name LIKE ? OR occ_date LIKE ? OR start LIKE ?)"] * len(words))
            for r in self._q(f"SELECT key, net_id, name, kind, start, stations, tx_count FROM inst WHERE tx_count>0 AND"
                             f" kind='net' AND {like} ORDER BY start DESC LIMIT 10",
                             [x for w_ in words for x in (f"%{w_}%",) * 3]):
                out["nets"].append(dict(r))
            if self.fts:
                ids = [r[0] for r in self._q("SELECT rowid FROM item_fts WHERE item_fts MATCH ? LIMIT 50",
                                             (fts_query(q),))]
                items = self._items(f"item.id IN ({','.join('?' * len(ids)) or 'NULL'}) AND item.status!="
                                    f"'dismissed'", ids)
            else:
                items = self._items(" AND ".join(["(item.text LIKE ? OR item.who LIKE ? OR item.place LIKE ?)"]
                                                 * len(words)) + " AND item.status!='dismissed'",
                                    [x for w_ in words for x in (f"%{w_}%",) * 3])
            if net:
                items = [i for i in items if i["net_id"] == net]
            for i in items:
                i["snippet"] = mark(i["text"], q, 200)
            out["items"] = list(reversed(items))[:30]
        return out

    # ---------- per recurring net ----------
    def analysis(self, net_id: str) -> dict:
        self.flush()
        insts = self._q("SELECT key, occ_date, start, ncs, stations, tx_count FROM inst WHERE net_id=? AND"
                        " kind='net' AND tx_count>0 ORDER BY occ_date", (net_id,))
        keys = [i["key"] for i in insts]
        n = self._net(net_id) or {}
        out = {"net_id": net_id, "name": n.get("name") or (self._one("SELECT name FROM inst WHERE net_id=? LIMIT 1",
                                                                     (net_id,)) or {"name": net_id})["name"],
               "summary": recur.summary(n) if n else None, "held": len(insts), "rule": REGULAR_RULE,
               "attendance": [], "stations": [], "regulars": [], "newcomers": [], "missing": [], "rota": []}
        if not insts:
            return out
        parts = self._q(f"SELECT inst, call, role FROM part WHERE inst IN ({','.join('?' * len(keys))})"
                        f" AND role IN ('ncs','checkin')", keys)
        by_inst: dict[str, set] = {k: set() for k in keys}
        for p in parts:
            by_inst[p["inst"]].add(p["call"])
        idx = {k: i for i, k in enumerate(keys)}
        calls: dict[str, list[int]] = {}
        for p in parts:
            calls.setdefault(p["call"], []).append(idx[p["inst"]])
        last8 = keys[-REGULAR_LAST:]
        names = {s["call"]: dict(s) for s in self._q(f"SELECT * FROM station WHERE call IN "
                                                f"({','.join('?' * len(calls)) or 'NULL'})", list(calls))}
        latest = keys[-1]
        rows = []
        for c, ii in calls.items():
            ii = sorted(set(ii))
            streak = 0
            for k in range(len(keys) - 1, -1, -1):
                if k in ii:
                    streak += 1
                else:
                    break
            recent = sum(1 for k in last8 if c in by_inst[k])
            regular = len(insts) >= 2 and len(ii) >= 2 and recent / len(last8) >= REGULAR_SHARE
            rows.append({"call": c, "name": (names.get(c) or {}).get("name"), "times": len(ii),
                         "share": round(len(ii) / len(keys), 3), "recent": recent, "of_recent": len(last8),
                         "first": insts[ii[0]]["occ_date"], "last": insts[ii[-1]]["occ_date"], "streak": streak,
                         "regular": regular, "ncs_times": sum(1 for i in insts if i["ncs"] == c)})
        rows.sort(key=lambda r: (-r["times"], r["call"]))
        out["stations"] = rows
        out["regulars"] = [r["call"] for r in rows if r["regular"]]
        out["newcomers"] = [r["call"] for r in rows if r["first"] == insts[-1]["occ_date"] and len(insts) > 1]
        out["missing"] = [r["call"] for r in rows if r["regular"] and r["call"] not in by_inst[latest]]
        out["attendance"] = [{"key": i["key"], "date": i["occ_date"], "count": len(by_inst[i["key"]]),
                              "ncs": i["ncs"]} for i in insts]
        rota: dict[str, dict] = {}
        for i in insts:
            if i["ncs"]:
                r = rota.setdefault(i["ncs"], {"call": i["ncs"], "times": 0, "last": None})
                r["times"] += 1; r["last"] = i["occ_date"]
        out["rota"] = sorted(rota.values(), key=lambda r: (-r["times"], r["call"]))
        out["latest"] = {"key": latest, "date": insts[-1]["occ_date"]}
        return out

    def station_nets(self, call: str) -> list[dict]:
        """Which recurring nets a callsign took part in, how often, first and last time (the callsign card)."""
        call = call.upper()
        rows = self._q("SELECT inst.net_id, inst.name, COUNT(*) times, MIN(inst.occ_date) first, MAX(inst.occ_date)"
                       " last FROM part JOIN inst ON inst.key=part.inst WHERE part.call=? AND part.role!='mentioned'"
                       " AND inst.kind='net' GROUP BY inst.net_id ORDER BY times DESC", (call,))
        out = []
        for r in rows:
            held = self._one("SELECT COUNT(*) n FROM inst WHERE net_id=? AND kind='net' AND tx_count>0",
                             (r["net_id"],))["n"]
            out.append(dict(r) | {"held": held})
        return out

    def still_open(self, net_id: str | None = None, at: datetime | None = None) -> dict:
        """What net control may want to read out: announcements still valid, upcoming events, open action items,
        traffic not yet handled and urgent issues still open (for one net, or every net)."""
        self.flush()
        today = (at or self.clock()).date()
        w, a = ["item.status='open'"], []
        if net_id:
            w.append("item.net_id=?"); a.append(net_id)
        else:
            w.append("item.net_id IS NOT NULL")
        items = self._items(" AND ".join(w), a)
        out = {"net_id": net_id, "announcements": [], "upcoming": [], "actions": [], "traffic": [], "urgent": []}
        for it in reversed(items):                                     # newest first, so a repeat keeps the newest
            day = date.fromisoformat((it.get("occ_date") or it.get("tx_start") or today.isoformat())[:10])
            if it["kind"] == "announcement":
                ok, why = minutes.still_valid(it, today, day)
                if ok and not any(minutes.similar(x["text"], it["text"]) for x in out["announcements"]):
                    out["announcements"].append(it | {"valid": why})
            if it["kind"] == "event" and (it.get("event_at") or "")[:10] >= today.isoformat():
                if not any(minutes.similar(x["text"], it["text"]) for x in out["upcoming"]):
                    out["upcoming"].append(it)
            if it["kind"] == "action":
                out["actions"].append(it)
            if it["kind"] == "traffic":
                out["traffic"].append(it)
            if it["kind"] == "urgent":
                out["urgent"].append(it)
        # a dated announcement is read out once, under Announcements, not again under Coming up
        out["upcoming"] = sorted((i for i in out["upcoming"] if not any(minutes.similar(x["text"], i["text"])
                                                                        for x in out["announcements"])),
                                 key=lambda i: i.get("event_at") or "")
        return out

    def open_counts(self, net_id: str, at: datetime | None = None) -> dict:
        s = self.still_open(net_id, at)
        return {k: len(s[k]) for k in ("announcements", "upcoming", "actions", "traffic", "urgent")}

    def urgent_open(self, days: int = 7) -> list[dict]:
        cut = (self.clock() - timedelta(days=days)).isoformat()
        return self._items("item.kind='urgent' AND item.status='open' AND COALESCE(item.tx_start, item.created)>=?",
                           (cut,))

    def _publish_urgent(self):
        if not self.bus:
            return
        u = self.urgent_open()
        sig = [(i["id"], i["status"], i["text"]) for i in u]
        if sig != self._urgent_sig:
            self._urgent_sig = sig
            self.bus.publish("urgent", {"urgent": u})

    # ---------- the operator's layer ----------
    def add_item(self, body: dict) -> dict:
        key = str(body.get("inst") or "")
        with self.lock:
            inst = self.con.execute("SELECT key, net_id FROM inst WHERE key=?", (key,)).fetchone()
            if not inst:
                raise LookupError(key)
            vals = self._clean_item(body, new=True)
            now = self.clock().isoformat(timespec="seconds")
            iid = self.con.execute(
                "INSERT INTO item(inst, net_id, kind, text, who, place, event_at, due, until, status, category, tx_id,"
                " tx_start, auto, edited, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?)",
                (key, inst["net_id"], vals["kind"], vals["text"], vals.get("who"), vals.get("place"),
                 vals.get("event_at"), vals.get("due"), vals.get("until"), vals.get("status", "open"),
                 vals.get("category"), body.get("tx_id"), body.get("tx_start"), now, now)).lastrowid
            self._fts_item(iid)
        self._publish_urgent()
        return self._items("item.id=?", (iid,))[0]

    def _clean_item(self, body: dict, new=False) -> dict:
        out = {}
        for k in ITEM_FIELDS:
            if k not in body:
                continue
            v = body[k]
            v = None if v in ("", None) else str(v).strip()
            if k == "kind" and v not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            if k == "status" and v not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            if k == "text" and not v:
                raise ValueError("an item needs some text")
            if k in ("due", "until") and v:
                v = date.fromisoformat(v[:10]).isoformat()
            if k == "event_at" and v:
                v = datetime.fromisoformat(v).isoformat(timespec="minutes") if "T" in v else \
                    date.fromisoformat(v).isoformat()
            if k == "text" and v and len(v) > 2000:
                v = v[:2000]
            out[k] = v
        if new and not out.get("text"):
            raise ValueError("an item needs some text")
        if new:
            out.setdefault("kind", "note")
        return out

    def update_item(self, iid: int, body: dict) -> dict:
        vals = self._clean_item(body)
        with self.lock:
            if not self.con.execute("SELECT 1 FROM item WHERE id=?", (iid,)).fetchone():
                raise LookupError(iid)
            if vals:
                sets = ", ".join(f"{k}=?" for k in vals)
                self.con.execute(f"UPDATE item SET {sets}, edited=1, updated=? WHERE id=?",
                                 list(vals.values()) + [self.clock().isoformat(timespec="seconds"), iid])
                self._fts_item(iid)
        self._publish_urgent()
        return self._items("item.id=?", (iid,))[0]

    def delete_item(self, iid: int) -> dict:
        """An item the operator added goes; a drafted one is dismissed (so the next draft does not bring it back)."""
        with self.lock:
            r = self.con.execute("SELECT auto FROM item WHERE id=?", (iid,)).fetchone()
            if not r:
                raise LookupError(iid)
            if r["auto"]:
                self.con.execute("UPDATE item SET status='dismissed', edited=1, updated=? WHERE id=?",
                                 (self.clock().isoformat(timespec="seconds"), iid))
            else:
                self.con.execute("DELETE FROM item WHERE id=?", (iid,))
                if self.fts:
                    self.con.execute("DELETE FROM item_fts WHERE rowid=?", (iid,))
        self._publish_urgent()
        return {"deleted": iid}

    def set_minutes(self, key: str, body: dict) -> dict:
        """The operator's notes, net control, and attendance corrections ({"add": call} / {"remove": call})."""
        with self.lock:
            if not self.con.execute("SELECT 1 FROM inst WHERE key=?", (key,)).fetchone():
                raise LookupError(key)
            m = self.con.execute("SELECT * FROM minutes WHERE inst=?", (key,)).fetchone()
            notes = m["notes"] if m else ""
            ncs = m["ncs"] if m else None
            added = json.loads(m["added"] or "[]") if m else []
            removed = json.loads(m["removed"] or "[]") if m else []
            if "notes" in body:
                notes = str(body["notes"] or "")[:20000]
            if "ncs" in body:
                ncs = str(body["ncs"] or "").strip().upper() or None
                if ncs and not re.fullmatch(r"[A-Z0-9]{3,8}(-\d{1,2})?", ncs):
                    raise ValueError("net control must be a callsign")
            for k, lst, other in (("add", added, removed), ("remove", removed, added)):
                c = str(body.get(k) or "").strip().upper()
                if c:
                    if not re.fullmatch(r"[A-Z0-9]{3,8}(-\d{1,2})?", c):
                        raise ValueError("not a callsign")
                    if c in other:
                        other.remove(c)
                    elif c not in lst:
                        lst.append(c)
            self.con.execute("INSERT OR REPLACE INTO minutes(inst, notes, ncs, added, removed, updated)"
                             " VALUES(?,?,?,?,?,?)", (key, notes, ncs, json.dumps(added), json.dumps(removed),
                                                      self.clock().isoformat(timespec="seconds")))
            self.con.execute("UPDATE inst SET dirty=1 WHERE key=?", (key,))
        return self.instance(key)

    # ---------- export ----------
    def minutes_text(self, key: str, md=True) -> str:
        d = self.instance(key)
        h = (lambda s: f"\n## {s}\n") if md else (lambda s: f"\n{s.upper()}\n")
        b = "- " if md else "  * "
        when = (d.get("start") or "")[:16].replace("T", " ")
        lines = [f"# {d.get('name') or 'Conversation'} — {d.get('occ_date') or when[:10]}" if md else
                 f"{d.get('name') or 'Conversation'} — {d.get('occ_date') or when[:10]}",
                 f"{when} to {(d.get('end') or '')[11:16]} local"
                 + (f", {d['freq_mhz']:.3f} MHz" if d.get("freq_mhz") else "")
                 + (f". Net control: {d['ncs']}" if d.get("ncs") else "") + f". {d.get('tx_count', 0)} transmissions."]
        if d.get("emergency"):
            lines.append("EMERGENCY / PRIORITY NET")
        roll = [p for p in d["participants"] if p["role"] in ("ncs", "checkin")]
        lines.append(h(f"Attendance ({len(roll)})"))
        for p in roll:
            lines.append(f"{b}{p['ord'] or ''}. {p['call']}" + (f" — {p['name']}" if p.get("name") else "")
                         + (" (net control)" if p["role"] == "ncs" else "") + f" {(p['first_at'] or '')[11:16]}")
        labels = [("urgent", "Urgent"), ("announcement", "Announcements"), ("event", "Schedule"),
                  ("action", "Action items"), ("decision", "Decisions"), ("traffic", "Traffic handled"),
                  ("note", "Notes")]
        for kind, label in labels:
            its = [i for i in d["items"] if i["kind"] == kind]
            if kind == "action":
                its += [i | {"carried": True} for i in d.get("carried", []) if i["kind"] == "action"]
            if not its:
                continue
            lines.append(h(label))
            for i in its:
                extra = [x for x in (
                    f"who: {i['who']}" if i.get("who") else "", f"due {i['due']}" if i.get("due") else "",
                    f"on {i['event_at'].replace('T', ' ')}" if i.get("event_at") else "",
                    f"until {i['until']}" if i.get("until") else "", f"at {i['place']}" if i.get("place") else "",
                    i.get("category") or "" if kind == "urgent" else "",
                    f"[{i['status']}]" if i["status"] != "open" or kind in ("action", "urgent") else "",
                    "(carried forward)" if i.get("carried") else "") if x]
                src = f" ({(i.get('tx_start') or '')[11:16]} {i.get('by') or ''})".rstrip() if i.get("tx_start") else ""
                lines.append(f"{b}{i['text']}" + (f" — {'; '.join(extra)}" if extra else "") + (src if md else ""))
        if d.get("notes"):
            lines.append(h("Notes"))
            lines.append(d["notes"])
        lines.append("\nDrafted from a machine transcription and reviewed by the operator.")
        return "\n".join(lines) + "\n"

    def summary_for_213(self, key: str) -> str:
        """The minutes, short, for the ICS-213 summary message."""
        d = self.instance(key)
        out = []
        for kind, label in (("urgent", "Urgent"), ("announcement", "Announcements"), ("event", "Upcoming"),
                            ("action", "Action items"), ("decision", "Decisions")):
            its = [i for i in d["items"] if i["kind"] == kind and i["status"] != "dismissed"]
            if its:
                out.append(f"{label}:\n" + "\n".join(
                    f"  - {i['text'][:160]}" + (f" ({i['who']})" if i.get("who") else "")
                    + (f" [{i['status']}]" if kind in ("urgent", "action") else "") for i in its))
        return "\n".join(out)
