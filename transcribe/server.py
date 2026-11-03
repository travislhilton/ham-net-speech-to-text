"""home server transcription service.

POST /transcribe            multipart: audio (WAV), meta (JSON) → transcript; row gets the open net's id
POST /nets/start            JSON {name, frequency_mhz?, net_control?, notes?, started_utc?} → net
POST /nets/{id}/stop        JSON {ended_utc?} → net; back-fills net_id on unassigned rows in its window
GET  /nets                  ?open=true for running nets only
POST /reload-vocab          re-read config/vocab.yaml + local.yaml (new roster without a restart)

Stores WAV under STT_DATA_DIR and the transcript in Postgres (optional; skipped if no DSN).
Every endpoint except /health requires X-API-Key when EMCOMM_STT_KEY is set."""
import json, os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

from . import nets as netlogic
from .engine import MODEL, Engine

DATA = Path(os.environ.get("STT_DATA_DIR", os.path.expanduser("~/ham-net-speech-to-text/data")))
API_KEY = os.environ.get("EMCOMM_STT_KEY")
DSN = os.environ.get("EMCOMM_DB_DSN")  # postgresql+psycopg://...

app = FastAPI(title="ham-net-speech-to-text transcribe")
engine = Engine()
_db = None
if DSN:
    from db.models import Net, Session, Transmission  # type: ignore
    _db = Session


def auth(x_api_key: str | None = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401)


def need_db():
    if not _db:
        raise HTTPException(503, "no database configured (EMCOMM_DB_DSN)")


def parse_utc(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def safe_name(name: str) -> str:
    n = Path(name or "").name
    if not n or n.startswith("."):
        raise HTTPException(400, "bad audio filename")
    return n


def _iso(dt):
    """Postgres hands back the session's local zone; the API speaks UTC."""
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def net_json(n) -> dict:
    return {"id": n.id, "name": n.name,
            "frequency_mhz": float(n.frequency_mhz) if n.frequency_mhz is not None else None,
            "started_utc": _iso(n.started_utc), "ended_utc": _iso(n.ended_utc),
            "net_control": n.net_control, "notes": n.notes}


def _open_nets(s, at: datetime):
    return s.query(Net).filter(Net.started_utc <= at,
                               (Net.ended_utc.is_(None)) | (Net.ended_utc >= at)).all()


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "accurate_model": ACCURATE_MODEL, "db": bool(_db)}


@app.post("/reload-vocab", dependencies=[Depends(auth)])
def reload_vocab():
    engine.reload()
    return {"ok": True, "roster": len(engine.roster)}


ACCURATE_MODEL = os.environ.get("STT_ACCURATE_MODEL", "mlx-community/whisper-large-v3-mlx")
_accurate_lock = __import__("threading").Lock()


@app.post("/accurate", dependencies=[Depends(auth)])
def accurate(audio: UploadFile = File(...), prompt: str = Form(""), highpass_hz: int = Form(300)):
    """The station's accurate second pass: one transmission in (16 kHz mono 16-bit WAV), text out. Nothing is
    stored here; the station keeps the result beside its live text. A plain def: FastAPI runs it in a worker
    thread, and the lock keeps one Whisper on the GPU at a time."""
    import io, time, wave
    import numpy as np
    import mlx_whisper
    with wave.open(io.BytesIO(audio.file.read())) as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise HTTPException(415, "send 16 kHz mono 16-bit WAV")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    if highpass_hz and len(x) > 64:                       # sub-audible tone + mains hum off an FM data jack
        f = np.fft.rfftfreq(len(x), 1 / 16000)
        g = 0.5 - 0.5 * np.cos(np.pi * np.clip((f - (highpass_hz - 100)) / 100, 0, 1))
        x = np.fft.irfft(np.fft.rfft(x.astype(np.float64)) * g, len(x)).astype(np.float32)
    t0 = time.time()
    with _accurate_lock:
        r = mlx_whisper.transcribe(x, path_or_hf_repo=ACCURATE_MODEL, language="en", initial_prompt=prompt or None,
                                   condition_on_previous_text=False, temperature=0)
    return {"text": r["text"].strip(), "model": ACCURATE_MODEL.split("/")[-1], "audio_s": round(len(x) / 16000, 2),
            "elapsed_s": round(time.time() - t0, 2)}


@app.post("/transcribe", dependencies=[Depends(auth)])
async def transcribe(audio: UploadFile = File(...), meta: str = Form("{}")):
    m = json.loads(meta)
    started_dt = parse_utc(m.get("started_utc"))
    d = DATA / started_dt.strftime("%Y%m%d")
    d.mkdir(parents=True, exist_ok=True)
    wav = d / safe_name(audio.filename)
    wav.write_bytes(await audio.read())
    wav.with_suffix(".json").write_text(json.dumps(m, indent=2))

    out = engine.transcribe(wav)
    out["net_id"] = None
    if _db:
        with _db() as s:
            net = netlogic.pick_net(_open_nets(s, started_dt), started_dt, m.get("frequency_mhz"))
            out["net_id"] = net.id if net else None
            tx = Transmission(net_id=out["net_id"], started_utc=started_dt,
                              duration_s=m.get("duration_s"), frequency_mhz=m.get("frequency_mhz"),
                              net_name=m.get("net_name") or (net.name if net else None),
                              station=m.get("station"), wav_path=str(wav), text=out["text"],
                              callsigns=out["callsigns"], model=out["model"], host=out["host"],
                              segments=out["segments"])
            s.add(tx); s.commit()
            out["id"] = tx.id
    wav.with_suffix(".transcript.json").write_text(json.dumps(out, indent=2))
    return out


@app.post("/nets/start", dependencies=[Depends(auth), Depends(need_db)])
def start_net(body: dict = Body(...)):
    if not body.get("name"):
        raise HTTPException(422, "name is required")
    with _db() as s:
        n = Net(name=body["name"], frequency_mhz=body.get("frequency_mhz"),
                started_utc=parse_utc(body.get("started_utc")),
                net_control=body.get("net_control"), notes=body.get("notes"))
        s.add(n); s.commit(); s.refresh(n)
        return net_json(n)


@app.post("/nets/{net_id}/stop", dependencies=[Depends(auth), Depends(need_db)])
def stop_net(net_id: int, body: dict = Body(default={})):
    with _db() as s:
        n = s.get(Net, net_id)
        if not n:
            raise HTTPException(404)
        if n.ended_utc is None:
            n.ended_utc = parse_utc(body.get("ended_utc"))
        if n.ended_utc < n.started_utc:
            raise HTTPException(422, "ended_utc is before started_utc")
        # Back-fill: clips uploaded late (puck queue drained after an outage) arrive after the
        # live assignment had its chance. Give each unassigned row in the window its best net.
        nets_in_window = s.query(Net).filter(Net.started_utc <= n.ended_utc,
                                             (Net.ended_utc.is_(None)) | (Net.ended_utc >= n.started_utc)).all()
        assigned = 0
        for t in s.query(Transmission).filter(Transmission.net_id.is_(None),
                                              Transmission.started_utc >= n.started_utc,
                                              Transmission.started_utc <= n.ended_utc):
            best = netlogic.pick_net(nets_in_window, t.started_utc, t.frequency_mhz)
            if best is not None and best.id == n.id:
                t.net_id = n.id; assigned += 1
        s.commit(); s.refresh(n)
        return {**net_json(n), "backfilled": assigned}


@app.get("/nets", dependencies=[Depends(auth), Depends(need_db)])
def list_nets(open: bool = False, limit: int = 50):
    with _db() as s:
        q = s.query(Net)
        if open:
            q = q.filter(Net.ended_utc.is_(None))
        return [net_json(n) for n in q.order_by(Net.started_utc.desc()).limit(limit)]
