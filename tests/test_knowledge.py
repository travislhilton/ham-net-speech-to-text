"""Sidekick's knowledge library (station/knowledge.py): offline, temp folders, a fake fetch for URLs."""
import io
import os
import threading
import zipfile

import pytest

from station import knowledge
from station.knowledge import Library, LibraryError

MANUAL = """# HAM Sidekick manual

Intro text about the station.

## Set level…

Set level sets the audio level just right, once per radio and cable. Step 1: key nothing and let the radio hiss;
step 2: talk into the radio. The recording level of the sound input is adjusted until the voice sits at the target.

## Winlink

The Winlink button opens the EOC form for a message over a VARA HF gateway.
"""


def _install(tmp_path):
    inst = tmp_path / "install"
    (inst / "docs").mkdir(parents=True)
    (inst / "station" / "static").mkdir(parents=True)
    (inst / "README.md").write_text("# HAM Sidekick Station\n\nA Windows station that transcribes nets.\n")
    (inst / "docs" / "MANUAL.md").write_text(MANUAL)
    (inst / "station" / "static" / "card.html").write_text(
        "<html><head><title>Card</title><style>.x{color:red}</style></head><body><h2>On a net</h2>"
        "<p>Press <b>Check in</b> to send your call sign to net control.</p><script>var y=1;</script></body></html>")
    return inst


@pytest.fixture(params=[True, False], ids=["fts5", "like"])
def lib(tmp_path, request):
    if request.param and not knowledge.fts5_available():
        pytest.skip("no FTS5 in this sqlite3")
    lb = Library(tmp_path / "data", _install(tmp_path), use_fts=request.param, auto_reindex_s=0)
    yield lb
    lb.close()


def _docx(paragraphs):
    """A minimal .docx: [(style or None, text)]."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    def ppr(st):
        return f'<w:pPr><w:pStyle w:val="{st}"/></w:pPr>' if st else ""
    body = "".join(f"<w:p>{ppr(st)}<w:r><w:t>{t}</w:t></w:r></w:p>" for st, t in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def test_builtin_docs_searchable(lib):
    hits = lib.search("um, how do I set the audio level")
    assert hits and hits[0]["source"] == "hamnet:docs/MANUAL.md"
    assert hits[0]["section"] == "Set level…" and "recording level" in hits[0]["text"]
    card = lib.search("check in net control")
    assert card[0]["source"] == "hamnet:station/static/card.html"
    assert "color:red" not in card[0]["text"] and "var y" not in card[0]["text"]
    assert {e["source"] for e in lib.list()} >= {"hamnet:README.md", "hamnet:docs/MANUAL.md"}


def test_builtin_reindexed_when_changed(lib, tmp_path):
    lib.reindex()
    m = tmp_path / "install" / "docs" / "MANUAL.md"
    m.write_text(MANUAL + "\n## Repeaters\n\nThe squelch tail of a repeater is never its own transmission.\n")
    os.utime(m, (1, 2_000_000_000))
    assert lib.search("repeater squelch tail")[0]["section"] == "Repeaters"


def test_note_and_files_found_with_their_source(lib):
    lib.add_note("Our EOC's Winlink gateways", "Primary gateway W9XYZ-10 on 145.050; backup K7ABC-10 on HF.")
    lib.add_file("roster.txt", b"Net control rotation: N0CALL first Tuesday, K7ABC second Tuesday.")
    lib.add_file("frequencies.md", b"# Simplex\n\nThe Springfield, IL simplex frequency is 146.520.")
    lib.add_file("shelter.html", b"<h1>Shelter</h1><p>The shelter generator runs 72 hours on one tank.</p>")
    lib.add_file("go-kit.docx", _docx([("Heading1", "Go kit"), (None, "Pack a spare battery and coax jumper.")]))
    lib.add_file("contacts.csv", b"role,call\nnet manager,W9XYZ\n")
    assert lib.search("winlink gateway")[0]["source"] == "note:Our EOC's Winlink gateways"
    assert lib.search("who is net control on the second tuesday")[0]["source"] == "file:roster.txt"
    assert lib.search("simplex frequency")[0]["source"] == "file:frequencies.md"
    assert lib.search("shelter generator")[0]["source"] == "file:shelter.html"
    g = lib.search("spare battery go kit")[0]
    assert g["source"] == "file:go-kit.docx" and g["section"] == "Go kit"
    assert "role: net manager; call: W9XYZ" in lib.search("net manager")[0]["text"]


def test_docx_without_python_docx_reads_the_xml(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda n, *a, **k: (_ for _ in ()).throw(ImportError()) if n == "docx" else real(n, *a, **k))
    text = knowledge.extract("x.docx", _docx([("Heading2", "Antennas"), (None, "Use the J-pole.")]))
    assert text == "## Antennas\n\nUse the J-pole."


def test_reindex_is_incremental(lib, monkeypatch):
    (lib.root / "a.txt").write_text("alpha bravo")
    (lib.root / "b.txt").write_text("charlie delta")
    first = lib.reindex()
    assert first["added"] >= 4
    reads = []
    orig = knowledge.extract
    monkeypatch.setattr(knowledge, "extract", lambda n, d: reads.append(n) or orig(n, d))
    again = lib.reindex()
    assert reads == [] and again["added"] == again["updated"] == 0
    (lib.root / "b.txt").write_text("charlie delta echo")
    assert lib.reindex()["updated"] == 1 and reads == ["b.txt"]
    os.utime(lib.root / "a.txt", (1, 2_000_000_000))      # touched, same bytes: read, not re-chunked
    assert lib.reindex()["updated"] == 0


def test_remove_drops_chunks(lib):
    lib.add_note("Fox hunt", "The fox transmits every five minutes.")
    assert lib.search("fox transmits")
    assert lib.remove("note:Fox hunt")
    assert not lib.search("fox transmits")
    assert "note:Fox hunt" not in {e["source"] for e in lib.list()}
    assert not list(lib.notes_dir.glob("*"))
    with pytest.raises(LibraryError):
        lib.remove("hamnet:docs/MANUAL.md")
    lib.add_file("x.txt", b"zulu yankee")
    lib.remove("x.txt")                       # by unique name
    assert not (lib.root / "x.txt").exists()


def test_broken_file_listed_and_rest_indexed(lib):
    (lib.root / "broken.docx").write_bytes(b"not a zip at all")
    (lib.root / "good.txt").write_text("the band plan for the drill")
    (lib.root / "slides.pptx").write_bytes(b"x")
    lib.reindex()
    by = {e["source"]: e for e in lib.list()}
    assert by["file:broken.docx"]["error"] and by["file:broken.docx"]["chunks"] == 0
    assert "not a supported type" in by["file:slides.pptx"]["error"]
    assert by["file:good.txt"]["error"] is None
    assert lib.search("band plan drill")[0]["source"] == "file:good.txt"
    entry = lib.add_file("bad.pdf", b"%PDF-garbage")
    assert entry["error"]            # pypdf missing, or a PDF it cannot parse: recorded, not raised


def test_url_fetch_and_refresh(lib):
    pages = {"v": "<title>EOC Winlink</title><h1>Gateways</h1><p>Use the VARA gateway on 40 meters.</p>"}
    calls = []

    def fetch(url):
        calls.append(url)
        return "text/html; charset=utf-8", pages["v"].encode()
    lib.fetch = fetch
    e = lib.add_url("https://example.org/winlink")
    assert e["source"] == "https://example.org/winlink" and e["name"] == "EOC Winlink" and e["chunks"] == 1
    hit = lib.search("VARA gateway")[0]
    assert hit["source"] == "https://example.org/winlink" and hit["section"] == "Gateways"
    pages["v"] = "<h1>Gateways</h1><p>Now the gateway is on 80 meters.</p>"
    lib.refresh("https://example.org/winlink")
    assert len(calls) == 2 and "80 meters" in lib.search("gateway meters")[0]["text"]
    lib.fetch = lambda url: ("application/pdf", b"%PDF")
    with pytest.raises(LibraryError, match="only web pages"):
        lib.add_url("https://example.org/x.pdf")
    lib.fetch = lambda url: ("text/plain", b"x" * (lib.max_url + 1))
    with pytest.raises(LibraryError, match="larger"):
        lib.add_url("https://example.org/big.txt")
    with pytest.raises(LibraryError):
        lib.add_url("ftp://example.org/")


def test_context_for_budget_and_labels(lib):
    for i in range(6):
        lib.add_note(f"Drill {i}", f"Drill {i}: the generator test " + "and fuel check " * 60)
    ctx = lib.context_for("generator test fuel", budget_chars=2000)
    assert 0 < len(ctx) <= 2000
    assert "[Drill " in ctx and " › " in ctx
    assert lib.context_for("xylophone quasar") == ""
    small = lib.context_for("generator test fuel", budget_chars=400)
    assert 0 < len(small) <= 400


def test_outline_groups_by_source(lib):
    lib.add_note("Winlink at the EOC", "Winlink messages go out over the VARA HF gateway.")
    lib.add_file("winlink.md", b"# Winlink\n\nWinlink Express is set up on the EOC laptop.\n\n"
                               b"# Forms\n\nWinlink forms include the ICS-213.")
    out = lib.outline("winlink")
    heads = [ln for ln in out.splitlines() if ln.startswith("## ")]
    assert len(heads) == len(set(heads)) >= 2
    assert out.index("Winlink Express") < out.index("ICS-213")       # each source in its own order
    assert len(lib.outline("winlink", budget_chars=300)) <= 300 + 200


def test_size_limits(tmp_path):
    lb = Library(tmp_path / "data", _install(tmp_path), auto_reindex_s=0, max_file=1000, max_total=2500)
    with pytest.raises(LibraryError, match="too large"):
        lb.add_file("big.txt", b"x " * 600)
    lb.add_file("a.txt", b"a " * 450)
    lb.add_file("b.txt", b"b " * 450)
    with pytest.raises(LibraryError, match="full"):
        lb.add_file("c.txt", b"c " * 450)
    lb.add_file("a.txt", b"a " * 450)         # replacing a file is not counted twice
    with pytest.raises(LibraryError, match="supported"):
        lb.add_file("evil.exe", b"MZ")
    (lb.root / "dropped.txt").write_bytes(b"d " * 600)     # copied into the folder by hand
    lb.reindex()
    assert "too large" in {e["source"]: e for e in lb.list()}["file:dropped.txt"]["error"]
    assert knowledge.MAX_FILE == 20 * 1024 ** 2
    assert knowledge.MAX_TOTAL == 200 * 1024 ** 2
    lb.close()


def test_upload_name_is_sanitized(lib):
    e = lib.add_file("..\\..\\evil/../notes.txt", b"plain words here")
    assert e["source"] == "file:notes.txt" and (lib.root / "notes.txt").exists()


def test_query_terms_for_speech():
    assert knowledge.query_terms("Um, so, uh, tell me about the E.O.C. and ARES") == [
        "eoc", "emergency operations center", "ares", "amateur radio emergency service"]
    assert "cw" in knowledge.query_terms("how fast is the CW") and "hf" in knowledge.query_terms("HF net")
    assert knowledge.query_terms("e o c") == ["eoc", "emergency operations center"]
    assert knowledge.fts_query(['say "hi"', "x"]) == '"say hi" OR "x"'


def test_chunking_sizes_and_overlap():
    text = "# Big\n\n" + "\n\n".join(f"Paragraph {i}. " + "word " * 50 for i in range(30))
    chunks = knowledge.chunk_text(text)
    assert len(chunks) > 3 and all(s == "Big" for s, _ in chunks)
    assert all(len(t) <= knowledge.CHUNK_MAX for _, t in chunks)
    assert all(len(t) >= knowledge.CHUNK_MIN for _, t in chunks[:-1])
    assert chunks[1][1].startswith("… ")


def test_threads_search_while_adding(lib):
    lib.reindex()
    errs = []

    def reader():
        try:
            for _ in range(30):
                lib.search("audio level")
        except Exception as e:      # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=reader) for _ in range(4)]
    for t in ts:
        t.start()
    for i in range(10):
        lib.add_note(f"Note {i}", f"text {i} about audio level")
    for t in ts:
        t.join()
    assert not errs


def test_damaged_index_rebuilt(tmp_path):
    lb = Library(tmp_path / "data", _install(tmp_path), auto_reindex_s=0)
    lb.reindex()
    lb.close()
    (tmp_path / "data" / "knowledge" / "index.db").write_bytes(b"garbage" * 100)
    lb2 = Library(tmp_path / "data", tmp_path / "install", auto_reindex_s=0)
    assert lb2.search("set the audio level")
    lb2.close()


def test_pdf_text_with_pypdf(lib):
    pytest.importorskip("pypdf")
    from pathlib import Path
    card = Path(__file__).resolve().parent.parent / "docs" / "HAM_Sidekick_Assistant_Quick_Reference_Card.pdf"
    e = lib.add_file("card.pdf", card.read_bytes())
    assert e["error"] is None and e["chunks"] > 0
    assert lib.search("sidekick check in")[0]["source"] == "file:card.pdf"
