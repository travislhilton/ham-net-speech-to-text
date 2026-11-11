# ham-net-speech-to-text — working notes for Claude

- **The product is a standalone Windows 10/11 station**: any PC, any GPU/CPU, one or more radios, works offline.
  A home server (Apple silicon, mlx-whisper, `transcribe/`) is an optional accurate second pass.
- **No personal details in this repo** (owner, 2026-11-03): no owner callsign, name, addresses, home-network or
  Tailscale IPs, host or user names, emails, SSH or other keys, or home-area place names — in code, tests, docs or
  examples. They live in config that is never committed: `config/station.yaml`, `config/local.yaml`,
  `config/secrets.env`, `capture/config.yaml`, `deploy/windows/ssh-keys.txt` (station) and
  `~/.config/emcomm-stt/env` (server). Examples use W9XYZ / K7ABC / N0CALL, Springfield, IL, 192.168.1.20.
  Also never: the home state, local club / net names, the real local net frequencies (measured results say "an HF
  LSB net", or use example values 3.915 / 442.100), ZIP codes, grid squares or coordinates. History was rewritten
  2026-11-04 to remove all of these (backup bundle on the home server, `~/data/backups/`); scan a commit's diff for
  them before pushing. Commits are authored as the GitHub no-reply address.
- **Owner's private notes:** if `private/` exists (a separate private repo cloned there, git-ignored), read
  `private/CLAUDE.md` and `private/continuation-prompt.md` first. Commit and push them in that repo.
- **README + installer evolve with every change**: update README "Status" and the install steps in the same
  commit as the feature; `install/windows/install.ps1` must stay idempotent.
- Tests: `.venv/bin/python -m pytest -q` from the repo root (496 green 2026-11-10).
- **The repo is PUBLIC** (MIT, https://github.com/travislhilton/ham-net-speech-to-text, since 2026-11-05): every push
  is published. The reference card's download link defaults to it (`station/phone.py` REPO_URL). Real callsigns
  heard on nets never go in tests: use calls checked unassigned in the FCC database.
- Whisper runs only through `transcribe/engine.py` on the server; the station's engines are in `station/engine.py`.
- OpenVINO on Windows: never create an `openvino.Core` in a process that runs a GPU WhisperPipeline — the
  process hangs forever at exit. List devices in a child process (`tools/pick_engine.py`).
- The station (`station/`) runs as task "HamNet Station" in the desktop session (audio + Credential Manager
  need it); restart it after code changes. Dashboard :8091.
- Windows over SSH: scp a script and run it with `-File` (piped stdin drops multi-line blocks).
- Capture never shares the digital-mode (VARA) audio device.
