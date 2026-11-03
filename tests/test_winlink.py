"""Winlink Express outbox: discovery, form building, the on-disk format, and the station's preview/send."""
import base64
import copy
import email
import time
from datetime import datetime, timezone
from email import policy

import numpy as np
import pytest

from station import engine, ics, winlink
from station.core import Station

T213 = """Form:ICS213_Initial.html,ICS213_Initial_Viewer.html
ReplyTemplate:ICS213_SendReply.0

To: 
Subject:ICS-213: <var Subjectline> - <var Mdate> <var mtime>

Readonly:True
Msg:
GENERAL MESSAGE (ICS 213)
<var IsExercise> 
1. Incident Name: <var inc_name>
2. To (Name and Position): <var To_Name>
3. From (Name and Position): <var fm_name>
4. Subject: <var Subjectline>
7. Message: 

<var Message>

8. Approved by: <var Approved_Name>
Express Sending Station: <MsgSender>
Senders Express Version: <ProgramVersion>
"""
T309 = "Form:Form-309_Initial.html,Form-309_Viewer.html\n\nSubject:Form 309- <var Title> - <var OpName> - <var OperId> - <var ActivityDateTime1> \nReadonly:True\n\nMsg:\n<var Title> - Form 309\n" + \
    "".join(f"TIME: <var Time{i}>\nFROM: <var From{i}>\nTO: <var To{i}>\nSUBJECT:\n <var Sub{i}>\n\n" for i in range(1, 31))


@pytest.fixture
def install(tmp_path):
    base = tmp_path / "RMS Express"
    (base / "Standard Templates" / "ICS USA Forms").mkdir(parents=True)
    (base / "Standard Templates" / "ICS USA Forms" / "ICS213 General Message.txt").write_text(T213, encoding="latin-1")
    (base / "Standard Templates" / "ICS USA Forms" / "Form-309 Communications Log.txt").write_text(T309, encoding="latin-1")
    (base / "RMS Express.exe").write_bytes(b"MZ")
    (base / "RMS Express.ini").write_text("[Main]\n[Properties]\nCallsign=W9XYZ-5\n[W9XYZ]\n")
    for acc in ("W9XYZ", "W9XYZ-5", "Global Folders"):
        for d in ("Messages", "Data"):
            (base / acc / d).mkdir(parents=True)
    (base / "W9XYZ-5" / "Data" / "Registry.txt").write_bytes(b"")
    return str(base)


def test_discovery(install):
    assert winlink.find_installs([install]) == [install]
    d = winlink.describe(install)
    assert d["accounts"] == ["W9XYZ", "W9XYZ-5"] and d["active"] == "W9XYZ-5" and d["templates"]


def test_grid_location_matches_express():
    assert winlink.grid_location("EM59et") == "39.812500N, 89.625000W (Grid square)"   # what Express wrote
    assert winlink.grid_location("nope") is None


def test_build_213_matches_express_shape(install):
    t = winlink.load_template(install, "ics213")
    assert t["viewer"] == "ICS213_Initial_Viewer.html" and "inc_name" in t["vars"]
    now = datetime(2026, 11, 2, 23, 38, 38, tzinfo=timezone.utc)
    m = winlink.build("ics213", t, {"inc_name": "TEST", "Subjectline": "Net summary", "Mdate": "2026-11-02",
                                    "mtime": "16:38", "Message": "Line one\nLine two é", "To_Name": "Net"},
                      "W9XYZ-5", ["W9XYZ", "K7ABC"], grid="EM59et", now=now, mid="ABCDEF123456")
    msg = email.message_from_bytes(m["mime"], policy=policy.compat32)
    assert msg["Subject"] == "ICS-213: Net summary - 2026-11-02 16:38" and msg["Message-ID"] == "ABCDEF123456"
    assert msg["To"] == "W9XYZ@winlink.org,\r\n K7ABC@winlink.org" and msg["From"] == "W9XYZ-5@winlink.org"
    assert b"To: W9XYZ@winlink.org,\r\n K7ABC@winlink.org\r\n" in m["mime"]          # folded, as Express writes it
    assert msg["X-Cancel"] == "2026/11/23 23:38" and msg["X-Location"].startswith("39.812500N")
    assert b"\r\n" in m["mime"] and b"\n\n" not in m["mime"].replace(b"\r\n", b"")
    parts = [p for p in msg.walk() if not p.is_multipart()]
    body = parts[0].get_payload(decode=True).decode("iso-8859-1")
    assert "1. Incident Name: TEST" in body and "Line two é" in body and "Express Sending Station: W9XYZ-5" in body
    assert parts[1].get_filename() == "RMS_Express_Form_ICS213_Initial_Viewer.xml"
    xml = parts[1].get_payload(decode=True).decode()
    assert "<display_form>ICS213_Initial_Viewer.html</display_form>" in xml and "<inc_name>TEST</inc_name>" in xml
    assert "<grid_square>EM59ET</grid_square>" in xml and "<reply_template>ICS213_SendReply.0</reply_template>" in xml


def test_values_fit_the_viewer_boxes(install, tmp_path):
    import pathlib
    v = pathlib.Path(install) / "Standard Templates" / "ICS USA Forms" / "Form-309_Viewer.html"
    v.write_text('<input readonly="readonly" value="{var Sub1}" maxlength="90" size="108" name="Sub1" />'
                 '<input value="{var Page}" maxlength="2" name="Page" />', encoding="latin-1")
    t = winlink.load_template(install, "ics309")
    assert t["limits"] == {"sub1": 90, "page": 2}
    m = winlink.build("ics309", t, {"Sub1": "x" * 150, "Page": "12"}, "W9XYZ-5", ["W9XYZ"])
    import re as _re
    xml = [p for p in email.message_from_bytes(m["mime"], policy=policy.compat32).walk() if not p.is_multipart()][1].get_payload(decode=True).decode()
    assert len(_re.search(r"<sub1>(.*?)</sub1>", xml).group(1)) == 90 and "<page>12</page>" in xml


def test_registry_line_has_express_fields(install):
    t = winlink.load_template(install, "ics309")
    m = winlink.build("ics309", t, {"Title": "Net"}, "W9XYZ-5", ["W9XYZ"], now=datetime(2026, 11, 2, 23, 37, 26, tzinfo=timezone.utc))
    f = winlink.registry_line(m, "W9XYZ-5").split("\x01")
    assert len(f) == 22 and f[1] == "2026/11/02 23:37" and f[8] == "Outbox" and f[16] == "W9XYZ" and f[19] == "20261102233726"
    two = winlink.build("ics309", t, {"Title": "Net"}, "W9XYZ-5", ["W9XYZ", "W9XYZ-5"])
    assert winlink.registry_line(two, "W9XYZ-5").split("\x01")[16] == "W9XYZ..."


def test_outbox_write_backup_and_refusal(install):
    t = winlink.load_template(install, "ics213")
    m = winlink.build("ics213", t, {"Subjectline": "x"}, "W9XYZ-5", ["W9XYZ"])
    with pytest.raises(RuntimeError):
        winlink.put_in_outbox(install, "W9XYZ-5", [m], check_running=lambda: True)
    r = winlink.put_in_outbox(install, "W9XYZ-5", [m], check_running=lambda: False)
    acc = f"{install}/W9XYZ-5"
    import pathlib
    p = pathlib.Path(acc)
    assert (p / "Messages" / f"{m['mid']}.mime").read_bytes() == m["mime"] == (p / "MessagesBackup" / f"{m['mid']}.mime").read_bytes()
    assert (p / "Data" / "Registry.txt").read_bytes().decode().endswith("\r\n") and m["mid"] in (p / "Data" / "Registry.txt").read_text()
    assert (p / "Data" / "Destinations.txt").read_bytes().startswith(b"\xef\xbb\xbf")
    assert list((p / "Data" / "Backup").glob("Registry_hamnet_*.txt")) and r["written"][0]["mid"] == m["mid"]
    m2 = winlink.build("ics213", t, {"Subjectline": "y"}, "W9XYZ-5", ["W9XYZ", "K7ABC"])
    winlink.put_in_outbox(install, "W9XYZ-5", [m2], check_running=lambda: False)
    dest = [l.lstrip("\ufeff").split("\x01") for l in (p / "Data" / "Destinations.txt").read_text(encoding="utf-8").splitlines() if m2["mid"] in l]
    assert [d[2] for d in dest] == ["W9XYZ", "K7ABC"]                                   # one line per recipient


def test_lzhuf_size_near_express():
    from station.lzhuf import compressed_size
    assert compressed_size(b"") == 6 and compressed_size(b"A" * 5000) < 200
    text = ("GENERAL MESSAGE (ICS 213)\r\n1. Incident Name: TEST\r\n" * 40).encode()
    assert compressed_size(text) < len(text) // 5


def test_session_bounds_and_drafts():
    tx = [{"radio": "a", "status": "final", "start": s, "speaker": sp, "callsigns": cs, "text": tx_, "hint_keys": hk}
          for s, sp, cs, tx_, hk in [
              ("2026-11-02T15:00:00", "K1AA", ["K1AA"], "old chat", []),
              ("2026-11-02T16:30:00", "WA7XQZ", ["WA7XQZ"], "net opens", []),
              ("2026-11-02T16:31:00", "W9XYZ", ["W9XYZ", "WA7XQZ"], "W9XYZ no traffic", []),
              ("2026-11-02T16:33:00", "K7ABC", ["K7ABC"], "relay for N0XYZ please", ["relay"])]]
    start, end = ics.session_bounds(tx, "a")
    assert start == "2026-11-02T16:30:00" and end == "2026-11-02T16:33:00"
    assert [s["count"] for s in ics.sessions(tx, "a")] == [1, 3]                       # 90-minute gap splits
    retuned = tx + [dict(tx[-1], start="2026-11-02T16:35:00", freq_mhz=146.94)]
    assert [(s["count"], s["freq_mhz"]) for s in ics.sessions(retuned, "a")][-1] == (1, 146.94)   # new frequency splits
    span = ics.select(tx, "a", start, end)
    people = ics.participants(span, {"W9XYZ"}, {"K7ABC": {"card": {"name": "Pat", "sources": ["FCC"]}}})
    assert [p["call"] for p in people] == ["WA7XQZ", "K7ABC"] and people[1]["confirmed"] and not people[0]["confirmed"]
    d = ics.drafts(span, {"freq_mhz": 145.9, "mode": "FM"}, {"callsign": "W9XYZ", "name": "Pat", "winlink_account": "W9XYZ-5"},
                   people, "check_in", "Springfield Valley net")
    assert [x["form"] for x in d] == ["ics309", "ics213"]
    v = d[0]["values"]
    assert v["Time2"] == "2026-11-02 16:31" and v["From2"] == "W9XYZ" and v["To2"] == "WA7XQZ" and v["Time30"] == ""
    assert "K7ABC — Pat" in d[1]["values"]["Message"] and "relay for N0XYZ" in d[1]["values"]["Message"]
    long = ics.drafts([dict(span[0], speaker=None, callsigns=["K7ABC", "N0XYZ"], text="word " * 60)], {}, {}, [], None, "T")
    r = long[0]["rows"][0]
    assert r["from"] == "?" and r["to"] == "NET" and len(r["sub"]) <= 90 and r["sub"].endswith("...")
    assert ics.fit("WB7XNI-12-EXTRA", 13) == "WB7XNI-12-..." and ics.fit("short", 13) == "short"
    old = ics.drafts([dict(span[0], freq_mhz=3.915)], {"freq_mhz": 3.985, "mode": "LSB"}, {}, [], None, None)
    assert old[0]["values"]["Title"] == "Net on 3.915 MHz"                             # the net's frequency, not today's
    many = [dict(tx[1], start=f"2026-11-02T16:{i:02d}:00") for i in range(40)]
    assert len([x for x in ics.drafts(many, {}, {}, [], None, "Big net") if x["form"] == "ics309"]) == 2


def test_station_settings_preview_and_send(tmp_path, install, monkeypatch):
    monkeypatch.setattr(winlink, "express_running", lambda: False)
    monkeypatch.setattr(winlink, "DEFAULT_DIR", install)
    cfg = {"callsign": "W9XYZ", "radios": [{"id": "a", "name": "FTM", "device": "x", "gate": "carrier", "freq_mhz": 145.9}],
           "schedule": [], "port": 0, "bind": "127.0.0.1", "qrz_online": False}
    st = Station(copy.deepcopy(cfg), lambda: engine.FakeEngine(["Net control, this is K7ABC checking in. Over."]), data_dir=tmp_path / "d")
    st.start(listen=False)
    try:
        op = st.operator()
        assert op["winlink_dir"] == install and op["winlink_account"] == "W9XYZ-5"
        with pytest.raises(ValueError):
            st.set_operator({"callsign": "not a call!"})
        st.set_operator({"callsign": "W9XYZ", "name": "Pat Sample", "grid": "EM59et"})
        st.roster.add("K7ABC")                     # no callbook in this test; the roster confirms the call
        st.inject("a", np.zeros(32000, np.float32))
        t0 = time.time()
        while not (st.tx and st.tx[-1]["status"] == "final") and time.time() - t0 < 5:
            time.sleep(0.05)
        p = st.winlink_preview("a")
        assert p["count"] == 1 and [x["call"] for x in p["to"]] == ["K7ABC"] and len(p["drafts"]) == 2
        r = st.winlink_send({"to": ["K7ABC"], "drafts": p["drafts"]})
        assert len(r["written"]) == 2 and r["account"] == "W9XYZ-5"
        assert r["written"][0]["subject"].startswith("Form 309- Net on 145.900 MHz FM - Pat Sample - W9XYZ-5")
        with pytest.raises(ValueError):
            st.winlink_send({"to": [], "drafts": p["drafts"]})
    finally:
        st.stop.set()
