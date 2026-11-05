# Architecture notes

## Decisions
- **The station is a standalone Windows PC** (`station/`): radios → carrier/voice gate → live Whisper text,
  callsign lookups, hints and net logs, with no network needed.
- **An optional home server** (`transcribe/`, FastAPI + mlx-whisper on Apple silicon) gives an accurate second
  pass and is the system of record (PostgreSQL + the WAV of every transmission).
- **An optional LLM server** answers only LLM calls (`analyze/`: per-transmission structuring and net
  summaries) through an OpenAI-compatible gateway such as a LiteLLM proxy. It is never on the live path:
  `analyze/llm.py` fails fast (`LLMUnavailable`, 120 s timeout, one retry) and `net_report` falls back to
  `analyze/rules.py` — an ICS-309 from the callsigns Whisper heard plus a roster, never written to the DB, so
  the LLM pass can run later.
- **Clip-per-transmission**, not one long recording. Each transmission becomes a DB row — the unit of an
  ICS-309 line. Gate = Silero VAD by default; a radio's carrier-detect line (e.g. Digirig COS on serial CTS)
  is more reliable on FM.
- **Second audio interface.** Never share the digital-mode (VARA) audio device.
- **Vocabulary-primed Whisper** (`config/vocab.yaml` + your `config/local.yaml`) + regex phonetic→callsign
  normalization, then LLM structuring with the roster for the residual.
- Every transcript row records `model` and `host`, so text made by a smaller fallback model can be re-run on
  the server later (`analyze/retranscribe.py`).

## Configuration (never committed)
| Where | What |
|---|---|
| `config/station.yaml` (station) | your callsign, radios, accurate-pass server URLs, download link |
| `config/local.yaml` | area, repeaters, roster, EOC names, QRZ login (or Windows Credential Manager) |
| `config/secrets.env` (station) | `EMCOMM_STT_KEY` for uploads to the server |
| `~/.config/emcomm-stt/env` (server, mode 600) | `EMCOMM_STT_KEY`, `EMCOMM_DB_DSN`, `STT_DATA_DIR`, optional `LITELLM_URL` / `LITELLM_KEY` |
| `capture/config.yaml` | the capture puck's device and upload settings |

Examples of each are in `config/station.example.yaml`, `capture/config.example.yaml` and
`deploy/my-server/install.sh`.

## LLM aliases
`analyze/` calls two model aliases on the gateway; map them to any models you run:
```yaml
- model_name: emcomm/structure     # per-transmission JSON (a small, fast model)
  litellm_params: {model: openai/<small-model>, api_base: http://<llm-server>:<port>/v1, api_key: not-needed}
- model_name: emcomm/summarize     # net-level ICS-309 + after-action summary (a larger model)
  litellm_params: {model: openai/<large-model>, api_base: http://<llm-server>:<port>/v1, api_key: not-needed}
```
Use standalone groups with no cloud fallback if transcripts must stay on your network. `analyze/`
authenticates with a per-app key set as `LITELLM_KEY` in the server's env file.

## Audio chain
Radio speaker/line out → USB audio interface (Digirig/SignaLink/plain dongle) → WASAPI, fixed 16 kHz mono.
Set radio volume so peaks hit about -6 dBFS; Whisper is tolerant but clipping is not recoverable.
