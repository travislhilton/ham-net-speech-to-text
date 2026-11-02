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
- starts the station at every sign-in (task "HamNet Station"), puts a **HamNet Station** shortcut on the
  desktop, and opens port 8091 to your home network and Tailscale only;
- lists the audio inputs it can record from.

## Connecting a radio

Each radio needs its receive audio on a PC sound input of its own (never the one a digital mode
such as VARA FM uses). Give that input a clear name in Windows: `Win+R` → `mmsys.cpl` → Recording →
the device → Properties → name, e.g. `NET-REC`.

| Radio | Interface | Notes |
|---|---|---|
| Yaesu FTM-150R (VHF/UHF) | DATA jack (10-pin mini-DIN) → Masters DRA-50M (USB) | Set 45 DATA BAND = A-BAND FIX, tune band A to the net; leave 41 DUAL RECEIVE MODE (a priority scan) OFF. The DATA jack is unsquelched, so the recorder's **carrier gate** detects transmissions from the drop in hiss. Step by step: `docs/FTM-150R-SETUP.txt`. |
| Icom IC-705 (HF/VHF/UHF, Wi-Fi) | planned | Over Wi-Fi via the radio's network remote (e.g. wfview → a virtual audio input, with frequency/mode read from the radio). SSB has no carrier to detect, so this radio will use the voice-activity gate. |

## Using it

After installing, the station starts by itself whenever you sign in to Windows. Open the
**HamNet Station** shortcut on the desktop, or browse to `http://localhost:8091/` on the station PC,
or `http://<station-name>:8091/` from another PC or phone on your home network or Tailscale.

The dashboard shows:
- **Radios**: each radio's frequency and band, a level meter, and **RX** while a station is transmitting.
- **On the air**: every transmission as it happens. Grey words appear while the station is still
  talking; the final text replaces them about 2–3 seconds after they unkey. Callsigns are buttons
  (the outlined one is the likely speaker), Q-codes and prowords are underlined with their meaning
  below, and ▶ plays the recording.
- **What you can do now**: step-by-step help and example phrases when someone asks for a relay,
  calls emergency or priority traffic, opens a net, takes check-ins or passes formal traffic, plus a
  reminder when your own 10-minute identification is due.
- **Stations heard**: name, location, license, grid and zones for each callsign (FCC, QRZ.com and the
  country file), and a box to look up any callsign.
- **Net type**: the kind of net being heard (ARES/RACES, SKYWARN, exercise, check-in, traffic, DX…),
  with a red banner if it sounds like a real emergency.
- **Schedule**: your nets, which radio each uses, and a warning if the radio is on another frequency.

Radios and nets are set in `C:\ham-net-speech-to-text\config\station.yaml` (every option is explained in
`config\station.example.yaml`). After editing it, restart the station: Task Scheduler → **HamNet Station** →
End, then Run (or sign out and back in). Everything heard is kept in `data\logs\<date>.jsonl` and
`data\recordings\<date>\`; problems are written to `data\station.log`.

Handy commands (PowerShell, from `C:\ham-net-speech-to-text`):
```powershell
.\.venv\Scripts\python.exe capture\recorder.py --list-devices      # find your radio's input
.\.venv\Scripts\python.exe capture\test_capture.py 30              # 30-second test: levels per second
.\.venv\Scripts\python.exe -m hamkit.fcc lookup W9XYZ K7ABC         # FCC callbook
.\.venv\Scripts\python.exe -m hamkit.dxcc lookup EA8AB JA1XYZ       # country of any callsign
.\.venv\Scripts\python.exe tools\pick_engine.py                    # re-measure after a hardware/driver change
```

## Status

| Piece | State |
|---|---|
| Recording per transmission (carrier gate for FM data jacks; voice gate otherwise) | working, tested on the FTM-150R |
| Weak-signal repetition guard (Whisper loops) | working |
| Icom IC-705 over Wi-Fi | planned |
| Live transcription on the PC (engine picked per PC; W9XYZ-5: turbo on Intel graphics, final text 2.4 s after unkey for an 8.5 s transmission) | working |
| Words while someone is still talking (base.en, updated each second, replaced by the final text) | working |
| FCC callbook (1.7 M licenses, daily updates) + country lookup for any callsign | working |
| QRZ.com online lookups (grid, international names), cached for offline use | working (`hamkit.qrz set-login`) |
| Q-code / proword / abbreviation explanations | working (`config/glossary.yaml`) |
| Operator hints: relay, check-in, emergency, formal traffic, exercise, CQ/DX, 10-minute ID | working (`config/procedures.yaml`) |
| Net type recognition (emergency, exercise, ARES/RACES, SKYWARN, NTS, check-in, DX) | working, rules-based |
| Live dashboard in the browser (on the station PC or any PC/phone on the network) | working (`station/`, port 8091) |
| Net schedule with radio, frequency and band | working, edited in `config/station.yaml`; an editor in the dashboard is next |
| Accurate second pass + ICS-309/213 reports (home network: home server + LLM server) | server side working |

## Editing the knowledge

- `config/glossary.yaml`: Q-codes, prowords, abbreviations and what they mean.
- `config/procedures.yaml`: the guidance cards (what triggers them, the steps, example phrases).
  `{me}` becomes your callsign, `{me_phonetic}` the phonetic spelling.
- `config/vocab.yaml` (+ your private `config/local.yaml`): callsigns and names that help Whisper.
- QRZ.com login (needs an XML Logbook Data subscription for full records). QRZ has no API key
  for lookups, so the password is stored in Windows Credential Manager / macOS Keychain, typed at a
  hidden prompt:
  ```powershell
  cd C:\ham-net-speech-to-text
  .\.venv\Scripts\python.exe -m hamkit.qrz set-login     # asks for callsign + password, then tests
  ```

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
