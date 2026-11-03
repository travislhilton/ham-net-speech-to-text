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
| Icom IC-705 (HF/VHF/UHF, Wi-Fi) | Icom **Remote Utility** (connected to the radio) → its virtual sound input `IC-705 (Icom Virtual Audio Driver)` | In `station.yaml`: `device: IC-705`, `gate: voice` (SSB has no carrier to detect). Remote Utility must be running and connected. Set the frequency and mode with **Change** on the radio's card; reading them from the radio (CAT) is planned. |

## Using it

After installing, the station starts by itself whenever you sign in to Windows. Open the
**HamNet Station** shortcut on the desktop, or browse to `http://localhost:8091/` on the station PC,
or `http://<station-name>:8091/` from another PC or phone on your home network or Tailscale.

The dashboard has one column per radio, with everything about that radio in it. The top of each column is
one set of tabs, **Radio** (meters, waterfall, frequency) | **What you can do now** |
**Stations heard**, all the same height, so **On the air**, the conversation, always fills the space below
and never moves when you switch tabs. A dot on "What you can do now" means it has guidance (red: urgent).
On the What you can do now and Stations heard tabs, drag the bar between the tabs and the conversation to
give either one more room (remembered per radio; double-click the bar to reset).
On a wide screen the page itself never scrolls; the conversation and the tab contents scroll on their own.
On a phone (or any window under 900 px wide) the radios are tabs under the header, one radio at a time; a
radio tab's dot turns green while that radio is receiving (red for emergency traffic), and the last radio
you chose is remembered. Each column shows:
- **Radios** (any number at once, each its own stream): frequency and band, **RX** while a station is
  transmitting, a live **Signal** meter (green = level in bounds; yellow = low or hot; red = clipping or
  almost no audio; the tick is the peak), an **SNR** meter while receiving (FM: noise quieting; SSB/AM:
  voice over the noise floor; green 20 dB+, yellow 10–20, red under 10), and a **waterfall** of the
  receiver's audio on the current frequency (the PC gets audio, not RF, so this is the audio passband,
  like WSJT-X/fldigi). Bounds can be changed per radio in `station.yaml` (`meter:`).
  When you retune a radio, press **Change** on its card and enter the new frequency and mode; every
  transmission after that is logged with it (kept across restarts in `data\\operator.json`).
- **On the air** (under each radio's card, one transcript per radio): every transmission as it happens.
  Grey words appear while the station is still
  talking; the final text replaces them about 2–3 seconds after they unkey. Callsigns are buttons
  (the outlined one is the likely speaker), Q-codes and prowords are underlined with their meaning
  below (untick **Explain radio terms** at the top to hide the meanings; on by default, remembered per
  browser), and ▶ plays the recording.
- **What you can do now** (per radio): step-by-step help and example phrases when someone asks for a relay,
  calls emergency or priority traffic, opens a net, takes check-ins or passes formal traffic, plus a
  reminder when your own 10-minute identification is due.
- **Stations heard** (per radio, a tab beside On the air): name, location, license, grid and zones for each
  callsign heard on that radio (FCC, QRZ.com and the country file). Click any callsign for its card; the box
  at the top of the page looks up any callsign.
- **Net type** (a badge on each radio card): the kind of net being heard (ARES/RACES, SKYWARN, exercise, check-in, traffic, DX…),
  with a red banner if it sounds like a real emergency.
- **Nets** (on each radio card): the nets scheduled on that radio; the one running now is in green.
- **Nets and conversations**: each radio's log is split into nets/conversations (a new one starts after 15
  quiet minutes or when the radio's frequency changes), each with a header: time span, frequency, how many
  transmissions and stations, and its own **Winlink messages…** button. That builds an **ICS-309**
  Communications Log (every transmission: time, from, to, what was said; 30 rows a page) and an **ICS-213**
  summary (stations heard, traffic and relays) for that net only, addressed to every callsign that took part
  (callsigns no callbook confirms start unticked; **Uncheck all / Check all / Only callbook-confirmed**, and add
  any callsign). Edit, then **Put in Winlink Express Outbox**; Winlink Express sends them the next time you
  connect. Close Winlink Express first; the station refuses while it is open and backs up Winlink's message
  list before each write.
- **Settings** (top right): your callsign, name and grid; which Winlink Express folder (found
  automatically: `C:\\RMS Express` and any other install) and which callsign account sends.

Which radios exist (and their sound inputs) and the nets are set in `C:\ham-net-speech-to-text\config\station.yaml` (every option is explained in
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
| Live signal + SNR meters and waterfall per radio; several radios at once | working |
| Recording per transmission (carrier gate for FM data jacks; voice gate otherwise) | working, tested on the FTM-150R |
| Weak-signal repetition guard (Whisper loops) | working |
| Icom IC-705 via Icom Remote Utility (voice gate) | working on W9XYZ-5; frequency read from the radio planned |
| Live transcription on the PC (engine picked per PC; W9XYZ-5: turbo on Intel graphics, final text 2.4 s after unkey for an 8.5 s transmission) | working |
| Words while someone is still talking (base.en, updated each second, replaced by the final text) | working |
| FCC callbook (1.7 M licenses, daily updates) + country lookup for any callsign | working |
| QRZ.com online lookups (grid, international names), cached for offline use | working (`hamkit.qrz set-login`) |
| Q-code / proword / abbreviation explanations | working (`config/glossary.yaml`) |
| Operator hints: relay, check-in, emergency, formal traffic, exercise, CQ/DX, 10-minute ID | working (`config/procedures.yaml`) |
| Net type recognition (emergency, exercise, ARES/RACES, SKYWARN, NTS, check-in, DX) | working, rules-based |
| Live dashboard in the browser (on the station PC or any PC/phone on the network) | working (`station/`, port 8091) |
| Winlink ICS-309 + ICS-213 into the Winlink Express Outbox, To: = everyone who took part | built; Outbox format matched against Winlink Express 1.8 messages (one and two recipients); first real write to be checked |
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
