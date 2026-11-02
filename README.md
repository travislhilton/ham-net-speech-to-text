# ham-net-speech-to-text

Live speech-to-text for amateur-radio voice nets and contacts, on a Windows 11 PC connected to one
or more radios. It shows who said what as it happens, explains Q-codes and prowords, looks up each
callsign, suggests what a newer operator can say next (check-in, relay, emergency traffic), and
recognizes what kind of net is on the air. It works with **no network** (portable station), and
when the home network is there it also hands recordings to a bigger machine for a more accurate
second pass and net reports (ICS-309 / ICS-213).

> **Status (2026-11-02): early build.** What works today is listed under "Status" below; this README
> is updated as each piece lands. Private repository.

## Install on a Windows 11 PC

1. On GitHub (signed in; the repo is private): **Code → Download ZIP**, then extract it anywhere.
2. Open the extracted folder → `install\windows` → double-click **`install.cmd`** and allow
   administrator access.
3. Type your callsign when asked. The first run downloads about 2.5 GB (Python packages, Whisper
   models, the FCC license database) and takes 10–20 minutes. Re-run `install.cmd` after every update.

What the installer does (`install/windows/install.ps1`, safe to re-run):
- installs Python 3.12 for all users (winget) and a private environment in `C:\ham-net-speech-to-text\.venv`;
- downloads three Whisper models in Intel OpenVINO format and **benchmarks this PC** to choose the
  live engine (`tools/pick_engine.py` → `config/engine.yaml`): Intel graphics, NVIDIA (CUDA) or CPU;
- downloads the FCC amateur license database and the world country file, and schedules a daily
  update (task "HamNet Callbook Update", 04:15, only when online);
- keeps your settings (`config/station.yaml`, `config/engine.yaml`, `config/local.yaml`) across updates;
- lists the audio inputs it can record from.

## Connecting a radio

Each radio needs its receive audio on a PC sound input of its own (never the one a digital mode
such as VARA FM uses). Give that input a clear name in Windows: `Win+R` → `mmsys.cpl` → Recording →
the device → Properties → name, e.g. `NET-REC`.

| Radio | Interface | Notes |
|---|---|---|
| Yaesu FTM-150R (VHF/UHF) | DATA jack (10-pin mini-DIN) → Masters DRA-50M (USB) | Set 45 DATA BAND = A-BAND FIX, tune band A to the net; leave 41 DUAL RECEIVE MODE (a priority scan) OFF. The DATA jack is unsquelched, so the recorder's **carrier gate** detects transmissions from the drop in hiss. Step by step: `docs/FTM-150R-SETUP.txt`. |
| Icom IC-705 (HF/VHF/UHF, Wi-Fi) | planned | Over Wi-Fi via the radio's network remote (e.g. wfview → a virtual audio input, with frequency/mode read from the radio). SSB has no carrier to detect, so this radio will use the voice-activity gate. |

## Using it (today)

```powershell
cd C:\ham-net-speech-to-text
.\.venv\Scripts\python.exe capture\recorder.py --list-devices      # find your radio's input
.\.venv\Scripts\python.exe capture\test_capture.py 30              # 30-second test: levels per second
.\.venv\Scripts\python.exe -m hamkit.fcc lookup W9XYZ K7ABC         # FCC callbook
.\.venv\Scripts\python.exe -m hamkit.dxcc lookup EA8AB JA1XYZ       # country of any callsign
```

## Status

| Piece | State |
|---|---|
| Recording per transmission (carrier gate for FM data jacks; voice gate otherwise) | working, tested on the FTM-150R |
| Live transcription on the PC (OpenVINO Whisper; turbo 2–4 s per transmission on an Intel i5-12600H's graphics) | engine measured; live service next |
| Words while someone is still talking (fast small model, replaced by the final text) | next |
| FCC callbook (1.7 M licenses, daily updates) + country lookup for any callsign | working |
| QRZ.com online lookups (photo, grid, international names) | needs a QRZ XML subscription; next |
| Q-code / proword / abbreviation explanations | working (`config/glossary.yaml`) |
| Operator hints: relay, check-in, emergency, formal traffic, exercise, CQ/DX, 10-minute ID | working (`config/procedures.yaml`) |
| Net type recognition (emergency, exercise, ARES/RACES, SKYWARN, NTS, check-in, DX) | working, rules-based |
| Live dashboard in the browser (on the station PC or any PC on the network) | designed (`docs/WEBAPP-DESIGN.md`) |
| Net schedule with radio, frequency and band | designed |
| Accurate second pass + ICS-309/213 reports (home network: home server + LLM server) | server side working |

## Editing the knowledge

- `config/glossary.yaml`: Q-codes, prowords, abbreviations and what they mean.
- `config/procedures.yaml`: the guidance cards (what triggers them, the steps, example phrases).
  `{me}` becomes your callsign, `{me_phonetic}` the phonetic spelling.
- `config/vocab.yaml` (+ your private `config/local.yaml`): callsigns and names that help Whisper.

## Home-network server (optional)

When the station is on the home network it can upload each transmission to the home server
(`transcribe/`, FastAPI + mlx-whisper) for the accurate pass, which stores nets and transmissions in
PostgreSQL; `analyze/` uses LLMs on the LLM server for structuring and summaries, and falls back to
rules when the LLM server is unreachable. `python -m tools.net start|stop|status` opens and closes a net.
Details: `docs/ARCHITECTURE.md`, deploy: `deploy/home-server/`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r transcribe/requirements.txt -r analyze/requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
Design and history: `docs/WEBAPP-DESIGN.md`, `docs/ARCHITECTURE.md`, `docs/HANDOFF.md`,
`continuation-prompt.md`.
