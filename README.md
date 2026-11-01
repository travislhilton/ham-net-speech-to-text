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
| `transcribe/` | home server | `server.py` — `POST /transcribe` (WAV → segments, callsign normalization, auto `net_id`), `POST /nets/start`, `POST /nets/{id}/stop`, `GET /nets`, `POST /reload-vocab`, `GET /health`; `engine.py` (the one Whisper path); `nets.py` (which net a clip belongs to); launchd plist in `deploy/home-server` |
| `analyze/` | home server (calls LLM server) | `structure.py` per-transmission JSON (speaker callsign, type, traffic), `net_report.py` → ICS-309 CSV/MD + summary, `retranscribe.py` re-runs stored WAVs (new roster/model) |
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

Tests: `pip install -r requirements-dev.txt && python -m pytest -q` (the DB tests create and drop
`emcomm_voice_test` on local Postgres 17 and stub Whisper; skipped when Postgres is absent).

## Running a net

```bash
H="X-API-Key: $EMCOMM_STT_KEY"   # value from ~/.config/emcomm-stt/env on the home server
curl -s -H "$H" -H 'content-type: application/json' \
  -d '{"name":"Springfield Valley ARES","frequency_mhz":146.94,"net_control":"W9XYZ"}' home-server:8090/nets/start
# ... every clip whose start falls inside the window (and on that frequency) gets the net's id ...
curl -s -H "$H" -H 'content-type: application/json' -d '{}' home-server:8090/nets/1/stop
python -m analyze.net_report --net-id 1
```
Both endpoints take an optional `started_utc` / `ended_utc` to log a net after the fact; stop
back-fills `net_id` on unassigned rows in the window. Overlapping nets are split by frequency
(±0.5 kHz); a net with no frequency takes any clip. After adding the roster:
`curl -X POST -H "$H" home-server:8090/reload-vocab`, then
`python -m analyze.retranscribe --since 2026-11-01 --dry-run` (drop `--dry-run` to write).

## Deploy (home server)

```bash
ssh home-server 'cd ~/Projects/ham-net-speech-to-text && git pull && deploy/home-server/install.sh'
```

`install.sh` is idempotent: python3.11 venv, requirements, `~/.config/emcomm-stt/env` (mode 600,
fresh `EMCOMM_STT_KEY` on first run — the puck needs the same value), `emcomm_voice` database and
schema, launchd `com.example.emcomm-stt` (port 8090, logs in `~/Library/Logs/example/`). The
plist in git holds no secrets; `run.sh` sources the env file. A private roster goes in
`config/local.yaml` (git-ignored, merged over `config/vocab.yaml`).

## Status
2026-11-01 — transcribe service deployed on the home server; `emcomm/*` aliases committed in
llm-platform. Net start/stop API + `retranscribe.py` live (17 tests). Puck capture not yet run against a real net. See `docs/HANDOFF.md`.
