# ham-net-speech-to-text

Speech-to-text capture, transcription and analysis of amateur-radio **voice nets** for
EMCOMM documentation (Springfield County IL / Shelbyville County IN). Companion to the Winlink
VARA FM station and the `winlink-forms` work.

## Topology (follows example-infrastructure conventions)

```
Radio (voice net freq) → 2nd USB audio interface → ACEMAGIC K1 puck (Win11)
   capture/   VAD/COS-gated recorder → one WAV + JSON per transmission
      │  HTTPS POST (LAN 192.168.1.0/24)
      ▼
home server (192.168.1.54)
   transcribe/  FastAPI + mlx-whisper (large-v3-turbo) → text + word timings
   db/          PostgreSQL schema: nets, transmissions, transcripts, callsigns
      │  LiteLLM alias emcomm/structure, emcomm/summarize
      ▼
LLM server (192.168.1.57) via LiteLLM :4000
   analyze/     roster extraction, ICS-213 traffic, ICS-309 comms log, AAR summary
```

Why the home server transcribes (not the LLM server): the LLM server is model-saturated and the
`home-server-leverage-plan` already places Whisper on Apple silicon (`mlx_whisper` is in
production for Manna). The puck keeps a CPU fallback (`faster-whisper` medium, int8) so
transcription survives a LAN/Mac outage — the EMCOMM case.

## Components

| Dir | Runs on | What |
|-----|---------|------|
| `capture/` | puck (Windows) | `recorder.py` — continuous listen on a named input device, Silero VAD (or Digirig COS) gates each transmission into `YYYYmmdd/HHMMSS.wav` + sidecar JSON; `uploader.py` ships clips and queues on failure |
| `transcribe/` | home server | `server.py` — `POST /transcribe` (WAV → segments, words, callsign normalization), `GET /health`; launchd plist in `deploy/home-server` |
| `analyze/` | home server (calls LLM server) | `structure.py` per-transmission JSON (speaker callsign, type, traffic), `net_report.py` → ICS-309 CSV/MD + summary |
| `db/` | home server | `schema.sql`, `models.py` (SQLAlchemy) |

## Quick start (dev, this dev laptop)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r transcribe/requirements.txt -r analyze/requirements.txt
uvicorn transcribe.server:app --port 8090          # test on a WAV
python -m analyze.net_report --net-id 1            # needs LiteLLM reachable
```

Puck: `pip install -r capture/requirements.txt`, then `python recorder.py --list-devices`,
set `config.yaml`, run as a Windows service via NSSM (`deploy/windows/`).

## Status
2026-10-31 — scaffolded. Nothing deployed. See `docs/ARCHITECTURE.md` and `CLAUDE.md`.
