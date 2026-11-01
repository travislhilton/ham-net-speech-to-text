"""home server transcription service. POST /transcribe (multipart: audio, meta JSON).
Stores WAV under STT_DATA_DIR and the transcript in Postgres (optional; skipped if no DSN)."""
import json, os, time
from datetime import datetime, timezone
from pathlib import Path

import mlx_whisper
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

from . import callsigns

MODEL = os.environ.get("STT_MODEL", "mlx-community/whisper-large-v3-turbo")
DATA = Path(os.environ.get("STT_DATA_DIR", os.path.expanduser("~/ham-net-speech-to-text/data")))
API_KEY = os.environ.get("EMCOMM_STT_KEY")
DSN = os.environ.get("EMCOMM_DB_DSN")  # postgresql+psycopg://...

app = FastAPI(title="ham-net-speech-to-text transcribe")
_prompt = callsigns.initial_prompt()
_roster = callsigns.load_roster()
_db = None
if DSN:
    from db.models import Session, Transmission  # type: ignore
    _db = Session


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "db": bool(_db)}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), meta: str = Form("{}"),
                     x_api_key: str | None = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401)
    m = json.loads(meta)
    started = m.get("started_utc") or datetime.now(timezone.utc).isoformat()
    started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=timezone.utc)
    day = started_dt.strftime("%Y%m%d")
    d = DATA / day
    d.mkdir(parents=True, exist_ok=True)
    wav = d / audio.filename
    wav.write_bytes(await audio.read())
    wav.with_suffix(".json").write_text(json.dumps(m, indent=2))

    t0 = time.time()
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=MODEL, language="en",
                               initial_prompt=_prompt, word_timestamps=True,
                               condition_on_previous_text=False)
    text, found = callsigns.normalize(r["text"].strip(), _roster)
    out = {
        "text": text,
        "callsigns": found,
        "segments": [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                      "avg_logprob": s.get("avg_logprob"), "no_speech_prob": s.get("no_speech_prob")}
                     for s in r["segments"]],
        "model": MODEL, "host": "home-server", "elapsed_s": round(time.time() - t0, 2),
        "wav": str(wav),
    }
    wav.with_suffix(".transcript.json").write_text(json.dumps(out, indent=2))
    if _db:
        with _db() as s:
            s.add(Transmission(started_utc=started_dt, duration_s=m.get("duration_s"),
                               frequency_mhz=m.get("frequency_mhz"), net_name=m.get("net_name"),
                               station=m.get("station"), wav_path=str(wav), text=text,
                               callsigns=found, model=MODEL, host="home-server",
                               segments=out["segments"]))
            s.commit()
    return out
