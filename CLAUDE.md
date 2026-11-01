# ham-net-speech-to-text — working notes for Claude

- Transcription host is the **home server** (mlx-whisper), NOT the LLM server. LLM server is reached only via
  LiteLLM `:4000` aliases `emcomm/structure` (light/Nemotron) and `emcomm/summarize` (heavy/Qwen).
  Register those aliases in `llm-platform/deploy/llm-server/litellm_config.yaml` as standalone
  groups per that repo's rules (never a fallback target to cloud).
- Capture never shares the VARA FM audio device. Second interface only.
- Every transcript row records `model` and `host` so puck-fallback transcripts can be re-run on
  the home server later (`analyze/retranscribe.py`). Whisper runs only through `transcribe/engine.py`.
- Tests: `.venv/bin/python -m pytest -q` from the repo root (17 green 2026-11-01).
- Keep the raw WAV forever (NAS warm tier). Transcripts are derived data.
- Callsign vocabulary: `config/vocab.yaml` (ITU phonetics, tacticals, EOC names, roster). Whisper
  `initial_prompt` is built from it; `callsigns.py` normalizes phonetic spellings back to callsigns.
- Deploy like the rest of the house: edit here → commit → push → pull on home server; launchd plist
  copied from `deploy/home-server/`, never edited in `~/Library/LaunchAgents` directly.

## Resume here
Read `continuation-prompt.md` first (current state, hosts, next steps), then `docs/HANDOFF.md`
(the design conversation), `docs/ARCHITECTURE.md` and `README.md`. Update the continuation
prompt at the end of every session.
