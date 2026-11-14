"""Sidekick's knowledge library: material it answers questions and gives prepared talks from, with sources.

Where it lives (the data folder is git-ignored; nothing here is ever committed):

    <data_dir>/knowledge/             the operator's own files: .md .txt .html .pdf .docx .csv (subfolders too)
    <data_dir>/knowledge/notes/       notes added on the dashboard (one .md each, first line "Title: ...")
    <data_dir>/knowledge/web/         pages fetched once from a URL (first line "URL: ...")
    <data_dir>/knowledge/index.db     the search index (SQLite FTS5, bm25; a cache: delete it and it is rebuilt)

Built in: this station's own documents from the install folder (README, manual, technical notes, architecture and
both reference cards), re-indexed whenever their file changes.

    lib = Library(data_dir, install_dir)
    lib.add_note("Our EOC's Winlink gateways", "Primary: W9XYZ-10 on 145.050 ...")
    lib.search("how do I set the audio level")        # -> [{id, source, section, text, score, kind, name}]
    lib.context_for(question)                          # the best chunks, labelled [name › section], for a prompt
    lib.outline("Winlink at the EOC")                  # more material, grouped by source, for a prepared talk

Indexing is incremental: a file whose size and modification time are unchanged is not read again, and one whose
bytes are unchanged is not chunked again. One bad file never stops the rest: its error is shown by list().
search(), list(), context_for() and outline() re-check the files at most every `auto_reindex_s` seconds.
Thread-safe: one lock guards the database, another serializes re-indexing (extraction runs outside the db lock).
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

# The station's own documents, relative to the install folder.
BUILTIN_DOCS = ("README.md", "docs/MANUAL.md", "docs/TECHNICAL.md", "docs/ARCHITECTURE.md",
                "station/static/card.html", "station/static/sidekick_card.html",
                "station/static/conversation_card.html")
FILE_TYPES = (".md", ".txt", ".html", ".htm", ".pdf", ".docx", ".csv")
MAX_FILE = 20 * 1024 ** 2         # one file
MAX_TOTAL = 200 * 1024 ** 2       # the operator's files, notes and pages together (built-in docs not counted)
MAX_URL = 2 * 1024 ** 2           # one fetched page
URL_TIMEOUT_S = 15
URL_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
CHUNK_MIN, CHUNK_TARGET, CHUNK_MAX, OVERLAP = 800, 1000, 1200, 150
SCHEMA = 1


class LibraryError(ValueError):                 # a 400 with its message on the dashboard
    """A request the library refuses (too large, unsupported type, a page that cannot be fetched...)."""


def fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
        c.close()
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------- text extraction

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head", "nav"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "section", "article", "table", "ul", "ol", "dl", "dt", "dd",
               "blockquote", "pre", "hr", "header", "footer", "main", "aside", "figure", "figcaption", "form"}


class _HTMLText(HTMLParser):
    """HTML -> markdown-ish text: headings become '#' lines (so sections survive), scripts and styles dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif not self.skip:
            if re.fullmatch(r"h[1-6]", tag):
                self.out.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("\n- ")
            elif tag in ("td", "th"):
                self.out.append(" ")
            elif tag in _BLOCK_TAGS:
                self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr") and not self.skip:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif not self.skip:
            if re.fullmatch(r"h[1-6]", tag):
                self.out.append("\n\n")
            elif tag in ("p", "table", "ul", "ol", "blockquote", "pre", "section", "article"):
                self.out.append("\n\n")
            elif tag in _BLOCK_TAGS:
                self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        lines = [ln.strip() for ln in "".join(self.out).splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """(title, text) of an HTML page."""
    p = _HTMLText()
    p.feed(html)
    p.close()
    return re.sub(r"\s+", " ", p.title).strip(), p.text()


def _decode(data: bytes, charset: str | None = None) -> str:
    for enc in ([charset] if charset else []) + ["utf-8-sig"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("cp1252", errors="replace")


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise LibraryError("PDF skipped: the pypdf package is not installed (pip install pypdf)") from None
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise LibraryError("PDF is password-protected") from None
    pages = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        if t:
            pages.append(f"# Page {i}\n\n{t}")
    if not pages:
        raise LibraryError("PDF has no text layer (a scanned image?)")
    return "\n\n".join(pages)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_text(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError:
        docx = None
    out = []
    if docx is not None:
        for p in docx.Document(io.BytesIO(data)).paragraphs:
            out.append(_docx_line(p.text, (p.style.name if p.style is not None else "") or ""))
    else:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
        for p in root.iter(_W + "p"):
            style = p.find(f"{_W}pPr/{_W}pStyle")
            style_name = style.get(_W + "val", "") if style is not None else ""
            text = "".join(t.text or "" for t in p.iter(_W + "t"))
            out.append(_docx_line(text, style_name))
    return "\n\n".join(x for x in out if x)


def _docx_line(text: str, style: str) -> str:
    text = text.strip()
    if not text:
        return ""
    m = re.match(r"heading\s*(\d)", style, re.I)
    if m:
        return "#" * min(6, int(m.group(1))) + " " + text
    if style.lower() == "title":
        return "# " + text
    return text


def _csv_text(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ""
    head = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        cells = [f"{head[i] if i < len(head) and head[i] else f'col{i + 1}'}: {v.strip()}"
                 for i, v in enumerate(r) if v.strip()]
        if cells:
            out.append("; ".join(cells))
    return "\n\n".join(out) if out else ", ".join(head)


def extract(name: str, data: bytes) -> str:
    """Markdown-ish text of a file (headings as '#' lines). Raises on a file it cannot read."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return _pdf_text(data)
    if ext == ".docx":
        return _docx_text(data)
    text = _decode(data)
    if ext in (".html", ".htm"):
        title, body = html_to_text(text)
        return body
    if ext == ".csv":
        return _csv_text(text)
    return text


# ---------------------------------------------------------------- chunking

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BOLD_ITEM = re.compile(r"^[-*]\s+\*\*([^*]{2,60})\*\*")


def _sections(text: str, first: str = "") -> list[tuple[str, str]]:
    """[(heading, body)] split at markdown headings (not inside ``` fences). A top-level bullet that leads with a
    bold name ("- **Set level…** (on each radio card): ...", the manual's feature list) starts a sub-section
    "heading › name", so one long list of features is not one long section."""
    out, head, base, buf, fence = [], first, first, [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else _HEADING.match(line)
        b = None if fence or m else _BOLD_ITEM.match(line)
        if m or b:
            if "\n".join(buf).strip():
                out.append((head, "\n".join(buf).strip()))
            buf = []
            if m:
                head = base = m.group(2).strip()
            else:
                name = b.group(1).strip().rstrip(":")
                head = f"{base} › {name}" if base else name
                buf.append(line)
        else:
            buf.append(line)
    if "\n".join(buf).strip():
        out.append((head, "\n".join(buf).strip()))
    return out


def _hard_split(s: str, limit: int) -> list[str]:
    out = []
    while len(s) > limit:
        cut = s.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    return out + ([s] if s else [])


def _pieces(para: str) -> list[tuple[str, str]]:
    """A paragraph as [(separator before it, piece)]: the whole paragraph if it fits in CHUNK_MAX, else its lines
    (an over-long line in sentences, an over-long sentence cut at a space). The first piece's separator is a blank
    line, so a chunk keeps the text's own paragraphs and line breaks."""
    if len(para) <= CHUNK_MAX:
        return [("\n\n", para)]
    out: list[tuple[str, str]] = []
    for line in para.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) <= CHUNK_MAX:
            out.append(("\n", line))
            continue
        cur, first = "", True
        for sent in re.split(r"(?<=[.!?;])\s+", line):
            parts = _hard_split(sent, CHUNK_TARGET) if len(sent) > CHUNK_MAX else [sent]
            for part in parts:
                if cur and len(cur) + 1 + len(part) > CHUNK_MAX:
                    out.append(("\n" if first else " ", cur))
                    cur, first = "", False
                cur = f"{cur} {part}" if cur else part
        if cur:
            out.append(("\n" if first else " ", cur))
    if out:
        out[0] = ("\n\n", out[0][1])
    return out


def _tail(text: str, n: int = OVERLAP) -> str:
    if len(text) <= n:
        return text
    t = text[-n:]
    sp = t.find(" ")
    return "… " + (t[sp + 1:] if 0 <= sp < n // 2 else t)


def chunk_text(text: str, first_section: str = "") -> list[tuple[str, str]]:
    """[(section heading, chunk text)]: ~800-1200 characters, cut at headings and paragraphs, a little overlap."""
    chunks = []
    for head, body in _sections(text, first_section):
        buf, fresh = "", True      # fresh: buf holds only the overlap from the previous chunk (nothing new yet)
        for para in re.split(r"\n\s*\n", body):
            for sep, piece in _pieces(para.strip()):
                if not piece:
                    continue
                if buf and not fresh and (len(buf) >= CHUNK_TARGET or len(buf) + len(sep) + len(piece) > CHUNK_MAX):
                    chunks.append((head, buf))
                    tail = _tail(buf)
                    buf = tail if len(tail) + 2 + len(piece) <= CHUNK_MAX else ""
                    fresh = True
                    sep = "\n\n"
                buf = f"{buf}{sep}{piece}" if buf else piece
                fresh = False
        if buf and not fresh:
            chunks.append((head, buf))
    return chunks


# ---------------------------------------------------------------- query handling

FILLER = set("""
um umm uh uhh er erm ah ahh hmm mm like so well okay ok yeah yes no please thanks thank you know i me my mine
the a an is are was were be been being do does did done how what when where which who whom why can could would
should will shall may might must to of in on at by for with from into about over under and or but if then than
it its this that these those there here our ours we us your yours they them their he she him her his hers
tell explain say said talk give show describe let lets get got go going want need any some all just also very
really please roger copy affirmative negative question questions again maybe kind sort thing
things stuff i'm im it's whats what's don't dont
""".split())

# Spoken / transcribed abbreviations: kept as words, a few given their long form too.
HAM_TERMS = {
    "eoc": ["eoc", "emergency operations center"],
    "ares": ["ares", "amateur radio emergency service"],
    "races": ["races", "radio amateur civil emergency service"],
    "vara": ["vara"],
    "rms": ["rms", "gateway"],
    "cw": ["cw", "morse"],
    "hf": ["hf"],
    "vhf": ["vhf"],
    "uhf": ["uhf"],
    "ics": ["ics"],
    "ptt": ["ptt", "push to talk"],
    "cat": ["cat"],
    "snr": ["snr"],
    "net": ["net"],
}


def query_terms(text: str) -> list[str]:
    """Search terms of a (transcribed) question: lower case, no filler, spelled letters joined ('E O C' -> eoc),
    ham abbreviations kept and a few expanded. Phrases come back as one string with a space."""
    words = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower().replace(".", " "))
    joined: list[str] = []
    run: list[str] = []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) >= 2:
            joined.append("".join(run))
        elif run:
            joined.append(run[0])
        run = []
        if w:
            joined.append(w)
    terms: list[str] = []
    for w in joined:
        if w in HAM_TERMS:
            adds = HAM_TERMS[w]
        elif w in FILLER or (len(w) < 2 and not w.isdigit()):
            continue
        else:
            adds = [w]
        for a in adds:
            if a not in terms:
                terms.append(a)
    if not terms:    # a question made only of small words: keep them rather than search for nothing
        terms = [w for w in dict.fromkeys(joined) if len(w) > 1]
    return terms


def fts_query(terms: list[str]) -> str:
    """An FTS5 OR query of the terms (each quoted, so no FTS syntax can come from speech)."""
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


# ---------------------------------------------------------------- the library

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", base).lstrip(". ")
    return base[:120]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "item"
    return f"{s}-{hashlib.sha1(text.encode()).hexdigest()[:6]}"


def _header(text: str) -> tuple[dict, str]:
    """'Key: value' lines up to the first blank line -> ({key: value}, rest)."""
    head, _, rest = text.partition("\n\n")
    meta = {}
    for line in head.splitlines():
        k, sep, v = line.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z]+", k.strip()):
            return {}, text
        meta[k.strip().lower()] = v.strip()
    return meta, rest


def default_fetch(url: str, timeout: float = URL_TIMEOUT_S, max_bytes: int = MAX_URL) -> tuple[str, bytes]:
    """(Content-Type, body) of a URL; at most max_bytes + 1 bytes are read."""
    req = urllib.request.Request(url, headers={"User-Agent": "HAM-Sidekick-Station knowledge library"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Content-Type", ""), r.read(max_bytes + 1)


class Library:
    def __init__(self, data_dir, install_dir, *, fetch=None, use_fts: bool | None = None,
                 auto_reindex_s: float = 30.0, max_file: int = MAX_FILE, max_total: int = MAX_TOTAL,
                 max_url: int = MAX_URL, builtin_docs=BUILTIN_DOCS):
        self.root = Path(data_dir) / "knowledge"
        self.install = Path(install_dir)
        self.notes_dir, self.web_dir = self.root / "notes", self.root / "web"
        for d in (self.root, self.notes_dir, self.web_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.fetch = fetch or default_fetch
        self.fts = fts5_available() if use_fts is None else bool(use_fts and fts5_available())
        self.auto_reindex_s = auto_reindex_s
        self.max_file, self.max_total, self.max_url = max_file, max_total, max_url
        self.builtin_docs = tuple(builtin_docs)
        self._db_lock = threading.RLock()
        self._index_lock = threading.RLock()
        self._last_index = None
        self.db_path = self.root / "index.db"
        try:
            self._open()
        except sqlite3.DatabaseError:       # a damaged index is only a cache: start it again
            self.db_path.unlink(missing_ok=True)
            self._open()

    # ---- database

    def _open(self):
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        ver = self.db.execute("PRAGMA user_version").fetchone()[0]
        fts_now = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='fts'").fetchone())
        if ver != SCHEMA or (ver and fts_now != self.fts):
            for t in ("fts", "chunks", "sources"):
                self.db.execute(f"DROP TABLE IF EXISTS {t}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, key TEXT UNIQUE, kind TEXT, name TEXT,
                url TEXT, mtime_ns INTEGER, size INTEGER, hash TEXT, chunks INTEGER DEFAULT 0, updated TEXT,
                indexed TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS chunks(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, source TEXT, seq INTEGER,
                section TEXT, text TEXT);
            CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);
        """)
        if self.fts:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(section, text, "
                            "tokenize='porter unicode61')")
        self.db.execute(f"PRAGMA user_version={SCHEMA}")
        self.db.commit()

    def close(self):
        with self._db_lock:
            self.db.close()

    def _drop_chunks(self, key: str):
        if self.fts:
            self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT rowid FROM chunks WHERE source=?)", (key,))
        self.db.execute("DELETE FROM chunks WHERE source=?", (key,))

    # ---- what is on disk

    def _walk(self) -> list[tuple[str, Path]]:
        """[(kind, path)] of everything that should be indexed."""
        # kind "hamnet" / keys "hamnet:<path>" are stored identifiers in each PC's library database: not renamed
        out = [("hamnet", self.install / rel) for rel in self.builtin_docs if (self.install / rel).is_file()]
        for kind, folder in (("note", self.notes_dir), ("url", self.web_dir)):
            out += [(kind, p) for p in sorted(folder.glob("*")) if p.is_file() and not p.name.startswith(".")]
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if (not p.is_file() or rel.parts[0] in ("notes", "web") or rel.name.startswith(("index.db", "~$"))
                    or rel.name.endswith(".part")
                    or any(part.startswith(".") for part in rel.parts)):
                continue
            out.append(("file", p))
        return out

    def _identity(self, kind: str, path: Path, data: bytes) -> tuple[str, str, str | None, str, str]:
        """(key, name, url, text, first section) of a source read from its bytes."""
        if kind == "hamnet":
            rel = path.relative_to(self.install).as_posix()
            return f"hamnet:{rel}", rel, None, extract(path.name, data), ""
        if kind == "file":
            rel = path.relative_to(self.root).as_posix()
            return f"file:{rel}", rel, None, extract(path.name, data), ""
        meta, body = _header(_decode(data))
        if kind == "note":
            title = meta.get("title") or path.stem
            return f"note:{title}", title, None, body, title
        url = meta.get("url") or path.stem
        title = meta.get("title") or url
        return url, title, url, body, title

    def _files_total(self, exclude: Path | None = None) -> int:
        total = 0
        for kind, p in self._walk():
            if kind != "hamnet" and p != exclude:
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    # ---- indexing

    def reindex(self, full: bool = False) -> dict:
        """Bring the index up to date with the files. Unchanged files (size + mtime) are not read; `full` reads
        everything again. Returns counts: {added, updated, unchanged, removed, errors}."""
        with self._index_lock:
            stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "errors": 0}
            with self._db_lock:
                known = {r["path"]: dict(r) for r in self.db.execute("SELECT * FROM sources")}
            seen, total = set(), 0
            for kind, path in self._walk():
                seen.add(str(path))
                res = self._index_path(kind, path, known.get(str(path)), full, total)
                total += res[1]
                stats[res[0]] += 1
            with self._db_lock:
                for p, row in known.items():
                    if p not in seen:
                        self._drop_chunks(row["key"])
                        self.db.execute("DELETE FROM sources WHERE path=?", (p,))
                        stats["removed"] += 1
                self.db.commit()
            self._last_index = time.monotonic()
            return stats

    def _index_path(self, kind: str, path: Path, row: dict | None, full: bool, total_before: int):
        """Index one file. Returns (stat name, bytes counted toward the library total)."""
        try:
            st = path.stat()
        except OSError as e:
            return self._record_error(kind, path, row, None, f"cannot read: {e}"), 0
        counted = 0 if kind == "hamnet" else st.st_size
        if row and not full and row["mtime_ns"] == st.st_mtime_ns and row["size"] == st.st_size:
            return ("errors" if row["error"] else "unchanged"), counted
        if kind == "file" and path.suffix.lower() not in FILE_TYPES:
            return self._record_error(kind, path, row, st, f"not a supported type ({', '.join(FILE_TYPES)})"), 0
        if kind != "hamnet" and st.st_size > self.max_file:
            return self._record_error(kind, path, row, st, f"too large: {st.st_size / 2 ** 20:.1f} MB "
                                      f"(the limit is {self.max_file / 2 ** 20:.0f} MB per file)"), 0
        if kind != "hamnet" and total_before + st.st_size > self.max_total:
            return self._record_error(kind, path, row, st, f"the library is full (the limit is "
                                      f"{self.max_total / 2 ** 20:.0f} MB in all)"), 0
        try:
            data = path.read_bytes()
            digest = hashlib.sha1(data).hexdigest()
            if row and not full and row["hash"] == digest and not row["error"]:
                with self._db_lock:
                    self.db.execute("UPDATE sources SET mtime_ns=?, size=? WHERE path=?",
                                    (st.st_mtime_ns, st.st_size, str(path)))
                    self.db.commit()
                return "unchanged", counted
            key, name, url, text, first = self._identity(kind, path, data)
            chunks = chunk_text(text, first)
            if not chunks:
                raise LibraryError("no text found in it")
        except Exception as e:     # one bad file never stops the rest
            msg = str(e) if isinstance(e, LibraryError) else f"could not read it ({type(e).__name__}: {e})"
            return self._record_error(kind, path, row, st, msg), counted
        updated = _iso(st.st_mtime)
        with self._db_lock:
            if row:
                self._drop_chunks(row["key"])
                self.db.execute("DELETE FROM sources WHERE path=?", (str(path),))
            key = self._free_key(key)
            self._drop_chunks(key)
            used = set()
            for seq, (section, chunk) in enumerate(chunks):
                cid = hashlib.sha1(f"{key}\0{section}\0{chunk}".encode()).hexdigest()[:16]
                n, base = 2, cid
                while cid in used:
                    cid, n = f"{base}-{n}", n + 1
                used.add(cid)
                cur = self.db.execute("INSERT OR REPLACE INTO chunks(id, source, seq, section, text) "
                                      "VALUES (?,?,?,?,?)", (cid, key, seq, section, chunk))
                if self.fts:
                    self.db.execute("INSERT INTO fts(rowid, section, text) VALUES (?,?,?)",
                                    (cur.lastrowid, section, chunk))
            self.db.execute("INSERT INTO sources(path, key, kind, name, url, mtime_ns, size, hash, chunks, updated, "
                            "indexed, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                            (str(path), key, kind, name, url, st.st_mtime_ns, st.st_size, digest, len(chunks),
                             updated, _now()))
            self.db.commit()
        return ("updated" if row else "added"), counted

    def _free_key(self, key: str) -> str:
        """`key`, or `key (2)`... if another file already holds it."""
        n, k = 2, key
        while self.db.execute("SELECT 1 FROM sources WHERE key=?", (k,)).fetchone():
            k, n = f"{key} ({n})", n + 1
        return k

    def _record_error(self, kind, path: Path, row, st, msg: str) -> str:
        if kind == "hamnet":
            rel = path.relative_to(self.install).as_posix()
            key, name = f"hamnet:{rel}", rel
        elif kind == "file":
            rel = path.relative_to(self.root).as_posix()
            key, name = f"file:{rel}", rel
        else:
            key, name = f"{kind}:{path.name}", path.name
        with self._db_lock:
            if row:
                self._drop_chunks(row["key"])
                self.db.execute("DELETE FROM sources WHERE path=?", (str(path),))
            key = self._free_key(key)
            self.db.execute("INSERT INTO sources(path, key, kind, name, url, mtime_ns, size, hash, chunks, updated, "
                            "indexed, error) VALUES (?,?,?,?,NULL,?,?,NULL,0,?,?,?)",
                            (str(path), key, kind, name, st.st_mtime_ns if st else None,
                             st.st_size if st else None, _iso(st.st_mtime) if st else None, _now(), msg))
            self.db.commit()
        return "errors"

    def _index_one(self, kind: str, path: Path) -> dict:
        with self._index_lock:
            with self._db_lock:
                r = self.db.execute("SELECT * FROM sources WHERE path=?", (str(path),)).fetchone()
            self._index_path(kind, path, dict(r) if r else None, True, self._files_total(exclude=path))
            with self._db_lock:
                r = self.db.execute("SELECT * FROM sources WHERE path=?", (str(path),)).fetchone()
            return self._entry(dict(r))

    def _auto(self):
        if self._last_index is not None and time.monotonic() - self._last_index < self.auto_reindex_s:
            return
        if self._index_lock.acquire(blocking=False):     # a re-index already running: search what is there
            try:
                self.reindex()
            finally:
                self._index_lock.release()

    # ---- adding and removing

    def _check_room(self, size: int, replacing: Path | None):
        if size > self.max_file:
            raise LibraryError(f"too large: {size / 2 ** 20:.1f} MB (the limit is "
                               f"{self.max_file / 2 ** 20:.0f} MB per file)")
        if self._files_total(exclude=replacing) + size > self.max_total:
            raise LibraryError(f"the library is full: it holds up to {self.max_total / 2 ** 20:.0f} MB; "
                               "remove something first")

    def add_file(self, name: str, data: bytes) -> dict:
        """Save an uploaded file into the library (a file of the same name is replaced) and index it.
        Returns its list() entry (with `error` if it could not be read). Refuses an unsupported type or size."""
        safe = _safe_name(name)
        if not safe or Path(safe).suffix.lower() not in FILE_TYPES:
            raise LibraryError(f"not a supported file type: {name!r} (use {', '.join(FILE_TYPES)})")
        path = self.root / safe
        self._check_room(len(data), path if path.exists() else None)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return self._index_one("file", path)

    def add_note(self, title: str, text: str) -> dict:
        """A short note with a title ("Our EOC's Winlink gateways"); the same title replaces the note."""
        title = re.sub(r"\s+", " ", title or "").strip()
        if not title or not (text or "").strip():
            raise LibraryError("a note needs a title and some text")
        body = f"Title: {title}\n\n{text.strip()}\n".encode()
        path = self.notes_dir / f"{_slug(title)}.md"
        self._check_room(len(body), path if path.exists() else None)
        path.write_bytes(body)
        return self._index_one("note", path)

    def add_url(self, url: str) -> dict:
        """Fetch a web page once (text/html or text/plain, up to 2 MB) and keep its text with the URL as its
        source. Adding the same URL again fetches it again (refresh). Raises LibraryError if it cannot be fetched."""
        url = (url or "").strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise LibraryError(f"not a web address: {url!r}")
        try:
            ctype, body = self.fetch(url)
        except Exception as e:
            raise LibraryError(f"could not fetch {url}: {e}") from None
        mime, _, params = (ctype or "").partition(";")
        mime = mime.strip().lower()
        if mime not in URL_TYPES:
            raise LibraryError(f"{url} is {mime or 'an unknown type'}; only web pages and plain text can be added "
                               "(download a PDF or document and add it as a file)")
        if len(body) > self.max_url:
            raise LibraryError(f"{url} is larger than {self.max_url / 2 ** 20:.0f} MB")
        m = re.search(r"charset=([\w-]+)", params, re.I)
        text = _decode(body, m.group(1) if m else None)
        title = ""
        if mime != "text/plain":
            title, text = html_to_text(text)
        if not text.strip():
            raise LibraryError(f"{url} has no text")
        title = re.sub(r"\s+", " ", title).strip()[:200]
        doc = f"URL: {url}\nTitle: {title or url}\nFetched: {_now()}\n\n{text}\n".encode()
        path = self.web_dir / f"{_slug(url)}.txt"
        self._check_room(len(doc), path if path.exists() else None)
        path.write_bytes(doc)
        return self._index_one("url", path)

    def refresh(self, source: str) -> dict:
        """Fetch a URL source again, or re-read a file / note now."""
        row = self._row(source)
        if row["kind"] == "url":
            return self.add_url(row["url"] or row["key"])
        return self._index_one(row["kind"], Path(row["path"]))

    def _row(self, source: str) -> dict:
        with self._db_lock:
            r = self.db.execute("SELECT * FROM sources WHERE key=?", (source,)).fetchone()
            if not r:
                rows = self.db.execute("SELECT * FROM sources WHERE name=? OR url=?", (source, source)).fetchall()
                if len(rows) == 1:
                    r = rows[0]
        if not r:
            raise LibraryError(f"no such source: {source!r}")
        return dict(r)

    def remove(self, source: str) -> bool:
        """Delete a file, note or page from the library (by its `source` key, or its name if that is unique).
        The station's built-in documents cannot be removed."""
        with self._index_lock:
            row = self._row(source)
            if row["kind"] == "hamnet":
                raise LibraryError("the station's own documents are always in the library")
            Path(row["path"]).unlink(missing_ok=True)
            with self._db_lock:
                self._drop_chunks(row["key"])
                self.db.execute("DELETE FROM sources WHERE path=?", (row["path"],))
                self.db.commit()
            return True

    # ---- reading

    @staticmethod
    def _entry(r: dict) -> dict:
        return {"source": r["key"], "kind": r["kind"], "name": r["name"], "url": r["url"], "size": r["size"],
                "chunks": r["chunks"], "updated": r["updated"], "error": r["error"]}

    def list(self) -> list[dict]:
        """Every source: {source, kind (hamnet|file|note|url), name, url, size, chunks, updated, error}."""
        self._auto()
        order = {"hamnet": 0, "note": 1, "file": 2, "url": 3}
        with self._db_lock:
            rows = [self._entry(dict(r)) for r in self.db.execute("SELECT * FROM sources")]
        return sorted(rows, key=lambda e: (order.get(e["kind"], 9), (e["name"] or "").lower()))

    def search(self, query: str, k: int = 6) -> list[dict]:
        """The k best chunks for a question: [{id, source, section, text, score, kind, name}], best first."""
        self._auto()
        terms = query_terms(query or "")
        if not terms or k <= 0:
            return []
        with self._db_lock:
            if self.fts:
                rows = self.db.execute(
                    "SELECT c.id, c.source, c.seq, c.section, c.text, -bm25(fts, 2.0, 1.0) AS score, s.kind, s.name "
                    "FROM fts JOIN chunks c ON c.rowid = fts.rowid JOIN sources s ON s.key = c.source "
                    "WHERE fts MATCH ? ORDER BY bm25(fts, 2.0, 1.0) LIMIT ?", (fts_query(terms), k)).fetchall()
                hits = [dict(r) for r in rows]
            else:
                hits = self._like_search(terms, k)
        for h in hits:
            h["score"] = round(float(h["score"]), 4)
        return hits

    def _like_search(self, terms: list[str], k: int) -> list[dict]:
        """Without FTS5: chunks holding any term (LIKE), scored tf-idf style (section counts double)."""
        stems = [_stem(t) for t in terms]
        where = " OR ".join(["lower(c.text) LIKE ? OR lower(c.section) LIKE ?"] * len(stems))
        args = [a for s in stems for a in (f"%{s}%", f"%{s}%")]
        rows = [dict(r) for r in self.db.execute(
            "SELECT c.id, c.source, c.seq, c.section, c.text, s.kind, s.name FROM chunks c "
            f"JOIN sources s ON s.key = c.source WHERE {where}", args)]
        n = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 1
        df = {s: sum(1 for r in rows if s in r["text"].lower() or s in r["section"].lower()) for s in stems}
        for r in rows:
            body, sec, score = r["text"].lower(), r["section"].lower(), 0.0
            for s in stems:
                tf = body.count(s) + 2 * sec.count(s)
                if tf:
                    score += math.log(1 + n / (df[s] or 1)) * tf / (tf + 1.2)
            r["score"] = score / (1 + len(body) / 4000)
        rows.sort(key=lambda r: -r["score"])
        return rows[:k]

    @staticmethod
    def label(hit: dict) -> str:
        return f"[{hit['name']} › {hit['section']}]" if hit.get("section") else f"[{hit['name']}]"

    def context_for(self, question: str, budget_chars: int = 6000) -> str:
        """The best chunks for a question, each under a [name › section] label, within budget_chars.
        Empty when nothing matches (the model should then say it has nothing on that)."""
        parts, used = [], 0
        for h in self.search(question, k=24):
            block = f"{self.label(h)}\n{h['text']}"
            if used + len(block) + 2 > budget_chars:
                room = budget_chars - used - 2
                if not parts and room > 200:          # the single best chunk, cut to fit
                    parts.append(block[: room - 1].rstrip() + "…")
                    used = budget_chars
                continue
            parts.append(block)
            used += len(block) + 2
        return "\n\n".join(parts)

    def outline(self, topic: str, budget_chars: int = 12000) -> str:
        """Material for a prepared talk on a topic: more chunks, grouped by source (best source first) and in
        each source's own order, within budget_chars."""
        hits = self.search(topic, k=60)
        picked, used = [], 0
        for h in hits:
            size = len(self.label(h)) + len(h["text"]) + 2
            if used + size > budget_chars:
                continue
            picked.append(h)
            used += size
        groups: dict[str, list[dict]] = {}
        for h in picked:                       # dicts keep insertion order: the best source's group comes first
            groups.setdefault(h["source"], []).append(h)
        out = []
        for hs in groups.values():
            hs.sort(key=lambda h: h["seq"])
            kind = {"hamnet": "station manual", "note": "note", "file": "file", "url": "web page"}.get(hs[0]["kind"])
            out.append(f"## {hs[0]['name']} ({kind})\n\n" + "\n\n".join(f"{self.label(h)}\n{h['text']}" for h in hs))
        return "\n\n".join(out)
