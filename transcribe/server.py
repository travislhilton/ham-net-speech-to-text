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

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile

from . import gpu
from . import nets as netlogic
from .engine import MODEL, Engine

DATA = Path(os.environ.get("STT_DATA_DIR", os.path.expanduser("~/ham-net-speech-to-text/data")))
API_KEY = os.environ.get("EMCOMM_STT_KEY")
DSN = os.environ.get("EMCOMM_DB_DSN")  # postgresql+psycopg://...

app = FastAPI(title="HAM Sidekick Station transcribe")


@app.on_event("startup")
def _gpu_on_demand():
    """Whisper loads on the first request and is unloaded when the requests stop (transcribe/gpu.py)."""
    gpu.configure()
    gpu.start_idle_watcher()

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
    return {"ok": True, "model": MODEL, "accurate_model": ACCURATE_MODEL, "db": bool(_db), **gpu.status()}


@app.post("/reload-vocab", dependencies=[Depends(auth)])
def reload_vocab():
    engine.reload()
    return {"ok": True, "roster": len(engine.roster)}


ACCURATE_MODEL = os.environ.get("STT_ACCURATE_MODEL", "mlx-community/whisper-large-v3-mlx")


def _wav16k(data: bytes):
    import io, wave
    import numpy as np
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
                raise HTTPException(415, "send 16 kHz mono 16-bit WAV")
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    except (wave.Error, EOFError) as e:
        raise HTTPException(400, f"not a WAV: {e}")


def _highpass(x, highpass_hz: int):
    """Sub-audible tone + mains hum off an FM data jack."""
    from .context import highpass
    return highpass(x, highpass_hz)


def _whisper_decode(model: str):
    """context.py's recogniser on the Mac: mlx_whisper (transcribe/decoders.py; station PCs use the OpenVINO or
    faster-whisper decoder there). No prompt and no conditioning on the previous window's text: with the previous text
    as a prompt large-v3 copied earlier callsigns into later transmissions (call precision 52 -> 42 % on the synthetic
    nets) and echoed prompts on the real ones (58 of 531).
    no_speech_threshold=None: FM data-jack hiss puts Whisper's no-speech probability at ~0.6 for whole 30 s windows,
    and its default skips such a window outright: on the 11-02 442.100 net a 294 s block lost 60 s of check-ins
    (89-150 s), 146 of 392 transmissions came back empty. Without the skip: 121 (mostly hiss the live text had read
    as "The wind is blowing up."), FCC-valid calls 82 -> 85. Loops, echoes and noise are caught in context.py."""
    from .decoders import mlx
    return mlx(model)


MAX_CONTEXT_AUDIO_S = 900        # one request: 15 min of audio (~29 MB); the station sends a net in blocks


@app.post("/accurate/context", dependencies=[Depends(auth)])
async def accurate_context(request: Request):
    """The accurate pass with context (transcribe/context.py). multipart: `manifest` (JSON) + one WAV per clip.
    manifest = {"clips": [{"id", "start" (epoch s or ISO), "file" (form field name), "target": bool}],
                "highpass_hz": 300, "stage": "provisional" | "final", "method": optional "long" | "windows" | "both"}
    The clips are put back on one timeline (pause-split pieces joined, real gaps marked by a short silence), decoded
    (final: Whisper's own sequential 30 s decode AND our windows cut at breaks, merged word by word by confidence;
    provisional: one decode), and each word goes back to the clip it was spoken in.
    The station decides the context: provisional = the transmission + the ~25 s before it; final = a block of the
    net + 30 s each side. -> {"results": {id: {"text", "conf", "flags", "words"}}, ...}
    Only target clips are returned; context clips are heard, not reported. Nothing is stored here."""
    import asyncio, time
    from . import context as cx
    form = await request.form()
    try:
        man = json.loads(form.get("manifest") or "{}")
    except ValueError:
        raise HTTPException(422, "manifest is not JSON")
    hp = int(man.get("highpass_hz", 300) or 0)
    stage = man.get("stage", "final")
    clips, audio_s = [], 0.0
    for c in man.get("clips", []):
        f = form.get(c.get("file") or "")
        if f is None or not hasattr(f, "read") or "id" not in c:
            raise HTTPException(422, f"clip {c.get('id')!r}: no audio part {c.get('file')!r}")
        start = c.get("start")
        if isinstance(start, str):
            start = datetime.fromisoformat(start).timestamp()
        x = _highpass(_wav16k(await f.read()), hp)
        audio_s += len(x) / 16000
        clips.append(cx.Clip(str(c["id"]), float(start), x, bool(c.get("target", True))))
    if not clips:
        raise HTTPException(422, "no clips")
    if audio_s > MAX_CONTEXT_AUDIO_S:
        raise HTTPException(413, f"{audio_s:.0f} s of audio in one request (max {MAX_CONTEXT_AUDIO_S})")
    # provisional: one decode of the transmission in its context (<= ~30 s: Whisper's long and window decodes are the
    # same thing there); final: Whisper's long decode + our break-cut windows, merged (transcribe/context.py)
    method = man.get("method") or ("both" if stage == "final" else "long")
    if method not in ("long", "windows", "both"):
        raise HTTPException(422, "method is long, windows or both")

    def run():
        t0 = time.time()
        with gpu.using():
            res = cx.transcribe_clips(clips, _whisper_decode(ACCURATE_MODEL), mode=method)
        return res, time.time() - t0
    res, took = await asyncio.get_running_loop().run_in_executor(None, run)
    return {"results": res, "model": ACCURATE_MODEL.split("/")[-1], "stage": stage, "audio_s": round(audio_s, 2),
            "elapsed_s": round(took, 2), "method": f"context-{method}"}


@app.post("/accurate", dependencies=[Depends(auth)])
def accurate(audio: UploadFile = File(...), prompt: str = Form(""), highpass_hz: int = Form(300)):
    """The station's accurate second pass: one transmission in (16 kHz mono 16-bit WAV), text out. Nothing is
    stored here; the station keeps the result beside its live text. A plain def: FastAPI runs it in a worker
    thread, and the lock keeps one Whisper on the GPU at a time. Kept for stations that predate /accurate/context."""
    import time
    import mlx_whisper
    x = _highpass(_wav16k(audio.file.read()), highpass_hz)
    t0 = time.time()
    with gpu.using():
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

    with gpu.using():
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
