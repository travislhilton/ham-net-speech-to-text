"""The read-out end to end with the companion project's REAL EOC server (church-emcomm-wnlk-forms, eoc/eoc_server.py).

Skipped unless EMCOMM_FORMS_REPO names a checkout of that project (it is a separate repository):

    EMCOMM_FORMS_REPO=~/src/church-emcomm-wnlk-forms .venv/bin/python -m pytest -q tests/test_forms_e2e.py

A scratch copy of the EOC server runs on a free port with its own database (nothing of a real EOC is touched). It is
seeded with invented reports, including details that must never be read on the air (injuries, a death, missionary
status, names, phones, GPS), then the station:
  * fetches GET /api/readout and accepts it (schema, fingerprint, no forbidden fields);
  * writes a script that says the facts and none of those details;
  * binds an approval to the fingerprint, and a new report with other facts clears it;
  * says in plain words when the EOC is not there.
Both projects carry the same contract (contracts/emcomm-readout here, docs/contracts/emcomm-readout there): the test
also checks the two copies are identical.
"""
import copy
import filecmp
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from station import engine
from station.core import Station
from tests.test_cat import CFG, Clock, TUE as T

FORMS = Path(os.path.expanduser(os.environ.get("EMCOMM_FORMS_REPO", ""))) if os.environ.get("EMCOMM_FORMS_REPO") else None
pytestmark = pytest.mark.skipif(not (FORMS and (FORMS / "eoc" / "eoc_server.py").exists()),
                                reason="set EMCOMM_FORMS_REPO to a church-emcomm-wnlk-forms checkout")
HERE = Path(__file__).resolve().parent.parent

NEVER = ("Field Worker", "555-01", "worker@example.org", "40.41", "-117.52", "Sisters not reached", "Crew Chief",
         "injur", "death", "dead", "missionar")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


@pytest.fixture()
def eoc(tmp_path):
    wd = tmp_path / "site" / "eoc"
    wd.mkdir(parents=True)
    for f in ("eoc_server.py", "EOC_Message_Center.html", "EOC_Wall_Display.html", "EOC_Phone.html", "readout.py"):
        if (FORMS / "eoc" / f).exists():
            shutil.copy2(FORMS / "eoc" / f, wd / f)
    if (FORMS / "templates").exists():
        shutil.copytree(FORMS / "templates", tmp_path / "site" / "templates")
    port = _free_port()
    log = open(tmp_path / "eoc.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", "eoc_server.py", str(port)], cwd=str(wd), stdout=log,
                            stderr=subprocess.STDOUT,
                            env=dict(os.environ, PYTHONIOENCODING="utf-8", EMCOMM_DISCOVERY="test",
                                     LOCALAPPDATA=str(tmp_path / "appdata"), USERPROFILE=str(tmp_path / "home"),
                                     HOME=str(tmp_path / "home"), EMCOMM_DOCUMENTS=str(tmp_path / "documents")))
    try:
        for _ in range(120):
            try:
                _call(port, "GET", "/api/status")
                break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("the EOC server did not start: " + (tmp_path / "eoc.log").read_text()[-800:])
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _seed(port, damaged="3"):
    ts = int(time.time() * 1000) - 3600000
    inc = _call(port, "GET", "/api/incident")
    inc.update({"eocName": "Larkspur Stake EOC", "eocLevel": "stake", "name": "Larkspur earthquake", "type": "Earthquake",
                "area": "Larkspur", "sitrepHrs": "4", "sitrepBy": "Stake President", "pace": "C", "paceTs": ts, "startTs": ts})
    _call(port, "PUT", "/api/incident", inc)
    dr = {"inc_stake": "Larkspur Stake", "inc_ward": "Larkspur 1st Ward", "inc_type": "Earthquake", "inc_date": "2026-11-10",
          "mem_injuries": "2", "mem_deaths": "1", "mem_damaged": damaged, "mem_destroyed": "1", "vol_count": "10",
          "vol_hours": "20", "miss_safe": "NO", "miss_details": "Sisters not reached", "need_local": "YES",
          "sub_name": "Field Worker One", "sub_phone": "555-0101", "sub_email": "worker@example.org",
          "stn_lat_d": "40.41", "stn_lon_d": "-117.52"}
    up = _call(port, "POST", "/api/upload", {"reports": [{"id": f"r-{damaged}", "form": "dr", "data": dr}]})
    assert up.get("new") == 1, up


def test_the_contract_copies_are_identical():
    ours, theirs = HERE / "contracts" / "emcomm-readout", FORMS / "docs" / "contracts" / "emcomm-readout"
    cmp = filecmp.dircmp(ours, theirs)
    def diffs(c):
        return c.diff_files + c.left_only + c.right_only + [x for s in c.subdirs.values() for x in diffs(s)]
    assert not diffs(cmp), f"contracts differ: {diffs(cmp)}"


def test_the_station_reads_the_real_eoc_server(eoc, tmp_path):
    _seed(eoc)
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path / "station", clock=Clock(T))
    v = st.readout.fetch(f"http://127.0.0.1:{eoc}/api/readout")
    doc = v["doc"]
    assert doc["source"]["eoc"] == {"name": "Larkspur Stake EOC", "level": "stake"}
    full = st.readout.s["doc"]                                             # the document as the EOC sent it
    assert full["property"]["homes_damaged"] == 3 and full["volunteers"]["count"] == 10
    facts = dict(v["facts"])
    assert any("3" in str(x) for k, x in facts.items() if "damaged" in k.lower()), facts
    assert v["role"] == "Stake President or his delegate" and v["approval"] is None
    said = " ".join(s["text"] for s in v["script"]).lower()
    assert "three" in said and "larkspur" in said
    leaked = [w for w in NEVER if w.lower() in json.dumps(full).lower() or w.lower() in said]
    assert not leaked, leaked
    v = st.readout.approve({"by": "Pat", "confirm_role": True, "fingerprint": doc["fingerprint"]})
    assert v["approval"]["by"] == "Pat"
    again = st.readout.fetch(f"http://127.0.0.1:{eoc}/api/readout")      # same facts: the approval stands
    assert again["approval"] and again["approval"]["by"] == "Pat"
    _seed(eoc, damaged="5")                                                # new facts at the EOC
    v = st.readout.fetch(f"http://127.0.0.1:{eoc}/api/readout")
    assert v["approval"] is None and v["revoked"]["changes"]


def test_an_eoc_that_is_not_there_is_said_plainly(tmp_path):
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path, clock=Clock(T))
    with pytest.raises(ValueError, match="Couldn't read the EOC"):
        st.readout.fetch(f"http://127.0.0.1:{_free_port()}/api/readout")
