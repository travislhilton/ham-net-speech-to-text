"""The HTTP service against a real, throwaway Postgres database (emcomm_voice_test). Whisper is stubbed:
these tests cover storage, auth and net assignment, not recognition. Skipped when Postgres is absent."""
import io, json, os, shutil, subprocess, wave
from pathlib import Path

import pytest

PG = "/opt/homebrew/opt/postgresql@17/bin"
DB = "emcomm_voice_test"
ROOT = Path(__file__).parent.parent


def _pg(*args):
    return subprocess.run([f"{PG}/{args[0]}", *args[1:]], capture_output=True, text=True)


if not Path(PG).exists() or _pg("pg_isready", "-q").returncode != 0:
    pytest.skip("local Postgres 17 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    _pg("dropdb", "--if-exists", DB)
    assert _pg("createdb", DB).returncode == 0
    assert _pg("psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", DB, "-f", str(ROOT / "db/schema.sql")).returncode == 0
    os.environ.update(EMCOMM_DB_DSN=f"postgresql+psycopg://{os.environ.get('USER')}@localhost:5432/{DB}",
                      EMCOMM_STT_KEY="test-key", STT_DATA_DIR=str(tmp_path_factory.mktemp("data")))
    import importlib, transcribe.server as server
    server = importlib.reload(server)

    def fake(wav):
        return {"text": f"heard {wav.name}", "callsigns": ["K7ABC"], "segments": [],
                "model": "stub", "host": "my-server", "elapsed_s": 0.0, "wav": str(wav)}
    server.engine.transcribe = fake
    from fastapi.testclient import TestClient
    c = TestClient(server.app, headers={"X-API-Key": "test-key"})
    yield c
    from db.models import engine
    engine.dispose()
    _pg("dropdb", "--if-exists", DB)


def _wav():
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 1600)
    return b.getvalue()


def upload(c, name, started, freq=None):
    r = c.post("/transcribe", files={"audio": (name, _wav(), "audio/wav")},
               data={"meta": json.dumps({"started_utc": started, "frequency_mhz": freq, "duration_s": 0.1})})
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_required(client):
    from fastapi.testclient import TestClient
    bare = TestClient(client.app)
    assert bare.get("/health").status_code == 200
    assert bare.get("/nets").status_code == 401
    assert bare.post("/transcribe", files={"audio": ("a.wav", _wav())}).status_code == 401


def test_path_traversal_filename_is_flattened(client):
    out = upload(client, "../../evil.wav", "2026-11-01T01:00:00Z")
    assert Path(out["wav"]).name == "evil.wav" and "/../" not in out["wav"]


def test_net_lifecycle_assigns_and_backfills(client):
    before = upload(client, "before.wav", "2026-11-01T01:59:00Z", 146.94)
    assert before["net_id"] is None

    n = client.post("/nets/start", json={"name": "Example County ARES", "frequency_mhz": 146.94,
                                         "net_control": "W9XYZ", "started_utc": "2026-11-01T02:00:00Z"}).json()
    assert client.get("/nets", params={"open": True}).json()[0]["id"] == n["id"]

    live = upload(client, "live.wav", "2026-11-01T02:05:00Z", 146.94)
    assert live["net_id"] == n["id"]
    other = upload(client, "other.wav", "2026-11-01T02:06:00Z", 145.29)
    assert other["net_id"] is None          # different frequency, not this net

    # A late clip (puck queue drained after the net) is assigned live too, since its start is in the window.
    late = upload(client, "late.wav", "2026-11-01T02:10:00Z", 146.94)
    assert late["net_id"] == n["id"]

    stopped = client.post(f"/nets/{n['id']}/stop", json={"ended_utc": "2026-11-01T02:30:00Z"}).json()
    assert stopped["ended_utc"].startswith("2026-11-01T02:30")
    assert client.get("/nets", params={"open": True}).json() == []

    after = upload(client, "after.wav", "2026-11-01T02:31:00Z", 146.94)
    assert after["net_id"] is None
    # Uploaded after the stop but started inside the window → assigned on arrival.
    queued = upload(client, "queued.wav", "2026-11-01T02:20:00Z", 146.94)
    assert queued["net_id"] == n["id"]


def test_stop_backfills_rows_inserted_without_a_net(client):
    from db.models import Session, Transmission
    n = client.post("/nets/start", json={"name": "Backfill", "started_utc": "2026-11-02T02:00:00Z"}).json()
    with Session() as s:   # a row written directly (e.g. imported) with no net
        s.add(Transmission(started_utc="2026-11-02T02:05:00Z", wav_path="/x.wav", text="x", host="import"))
        s.commit()
    r = client.post(f"/nets/{n['id']}/stop", json={"ended_utc": "2026-11-02T02:30:00Z"}).json()
    assert r["backfilled"] == 1


def test_stop_unknown_and_backwards(client):
    assert client.post("/nets/99999/stop", json={}).status_code == 404
    n = client.post("/nets/start", json={"name": "Oops", "started_utc": "2026-11-03T02:00:00Z"}).json()
    assert client.post(f"/nets/{n['id']}/stop", json={"ended_utc": "2026-11-03T01:00:00Z"}).status_code == 422
    assert client.post("/nets/start", json={}).status_code == 422


def test_transcript_sidecar_records_row_and_net(client):
    out = upload(client, "side.wav", "2026-11-04T01:00:00Z")
    side = json.loads(Path(out["wav"]).with_suffix(".transcript.json").read_text())
    assert side["id"] == out["id"] and "net_id" in side


def test_retranscribe_updates_stale_rows_and_clears_structure(client, monkeypatch, capsys):
    from db.models import Session, Transmission
    import transcribe.engine as eng_mod
    from analyze import retranscribe
    out = upload(client, "rerun.wav", "2026-11-05T01:00:00Z")
    with Session() as s:
        t = s.get(Transmission, out["id"]); t.model, t.host, t.structured = "small", "puck", {"kind": "x"}; s.commit()

    class NewVocab:
        model, roster = "stub", set()
        def __init__(self): pass
        def transcribe(self, wav):
            return {"text": "K7ABC checking in", "callsigns": ["K7ABC"], "segments": [],
                    "model": "stub", "host": "my-server", "elapsed_s": 0.0, "wav": str(wav)}
    monkeypatch.setattr(eng_mod, "Engine", NewVocab)

    retranscribe.main(["--id", str(out["id"]), "--dry-run"])
    with Session() as s:
        assert s.get(Transmission, out["id"]).host == "puck"          # dry run wrote nothing
    retranscribe.main(["--stale"])
    with Session() as s:
        t = s.get(Transmission, out["id"])
        assert (t.text, t.host, t.model, t.structured) == ("K7ABC checking in", "my-server", "stub", None)
    assert "changed" in capsys.readouterr().out


def test_accurate_pass_endpoint(client, monkeypatch):
    """The station's second pass: WAV in, text out, nothing stored; auth required; wrong format refused."""
    import sys, types
    seen = {}

    def fake_transcribe(x, **kw):
        seen.update(kw, n=len(x))
        return {"text": " Net control, this is K7ABC. "}
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe))
    r = client.post("/accurate", files={"audio": ("a.wav", _wav(), "audio/wav")}, data={"prompt": "Ham radio net."})
    assert r.status_code == 200 and r.json()["text"] == "Net control, this is K7ABC." and r.json()["audio_s"] == 0.1
    assert seen["initial_prompt"] == "Ham radio net." and seen["n"] == 1600 and seen["temperature"] == 0
    from fastapi.testclient import TestClient
    import transcribe.server as server
    assert TestClient(server.app).post("/accurate", files={"audio": ("a.wav", _wav())}).status_code == 401
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\0\0\0\0" * 100)
    assert client.post("/accurate", files={"audio": ("s.wav", b.getvalue())}).status_code == 415
