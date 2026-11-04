# ham-net-speech-to-text — working notes for Claude

- **The product is a standalone Windows 10/11 station**: any PC, any GPU/CPU, one or more radios, works offline.
  A home server (Apple silicon, mlx-whisper, `transcribe/`) is an optional accurate second pass.
- **No personal details in this repo** (owner, 2026-11-03): no owner callsign, name, addresses, home-network or
  Tailscale IPs, host or user names, emails, SSH or other keys, or home-area place names — in code, tests, docs or
  examples. They live in config that is never committed: `config/station.yaml`, `config/local.yaml`,
  `config/secrets.env`, `capture/config.yaml`, `deploy/windows/ssh-keys.txt` (station) and
  `~/.config/emcomm-stt/env` (server). Examples use W9XYZ / K7ABC / N0CALL, Springfield, 192.168.1.20.
- **Owner's private notes:** if `private/` exists (a separate private repo cloned there, git-ignored), read
  `private/CLAUDE.md` and `private/continuation-prompt.md` first. Commit and push them in that repo.
- **README + installer evolve with every change**: update README "Status" and the install steps in the same
  commit as the feature; `install/windows/install.ps1` must stay idempotent.
- Tests: `.venv/bin/python -m pytest -q` from the repo root (307 green 2026-11-03).
- Whisper runs only through `transcribe/engine.py` on the server; the station's engines are in `station/engine.py`.
- OpenVINO on Windows: never create an `openvino.Core` in a process that runs a GPU WhisperPipeline — the
  process hangs forever at exit. List devices in a child process (`tools/pick_engine.py`).
- The station (`station/`) runs as task "HamNet Station" in the desktop session (audio + Credential Manager
  need it); restart it after code changes. Dashboard :8091.
- Windows over SSH: scp a script and run it with `-File` (piped stdin drops multi-line blocks).
- Capture never shares the digital-mode (VARA) audio device.
