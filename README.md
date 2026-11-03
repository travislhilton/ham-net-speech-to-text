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

What the installer does (`install/windows/install.ps1`, safe to re-run). Nothing needs to be installed beforehand:
- checks the PC first (64-bit Windows 10/11, about 6 GB free, internet for the first install) and says what is missing;
- installs the Microsoft Visual C++ runtime if it is missing (OpenVINO, PyTorch and faster-whisper need it);
- installs Python 3.12 for all users (winget, or straight from python.org when winget is missing) and a private
  environment in `C:\ham-net-speech-to-text\.venv` with every Python package the station uses;
- downloads three Whisper models in Intel OpenVINO format and **benchmarks this PC** to choose the
  live engine (`tools/pick_engine.py` → `config/engine.yaml`): Intel graphics, NVIDIA (CUDA) or CPU;
- downloads the FCC amateur license database and the world country file, and schedules a daily
  update (task "HamNet Callbook Update", 04:15, only when online);
- asks (once, optional) for a QRZ.com login: Enter skips it. It checks the login and says in plain words what the
  account gives (full records need QRZ's "XML Logbook Data" subscription; without it only names and countries; US
  callsigns are covered by the FCC database either way). The password goes to Windows Credential Manager;
- reports the graphics card and driver (Intel/NVIDIA graphics are used only with a current driver);
- keeps your settings (`config/station.yaml`, `config/engine.yaml`, `config/local.yaml`) across updates;
- starts the station at every sign-in (task "HamNet Station"), puts a **HamNet Station** shortcut on the
  desktop, and opens port 8091 to your home network and Tailscale only;
- lists the audio inputs it can record from.

## Your own settings stay on your PC

Everything personal is kept in files the repository never contains (they are git-ignored and the installer
never overwrites them): your callsign, name and grid and the radios in `config\station.yaml`; your area, repeaters
and roster in `config\local.yaml`; the accurate-pass server's address in `station.yaml` (`accurate: urls:`) and its
key in `config\secrets.env`; the QRZ.com password in Windows Credential Manager; SSH public keys for remote
management in `deploy\windows\ssh-keys.txt`. The server keeps its settings in `~/.config/emcomm-stt/env`.

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
- **Pause / Resume** (on each radio card): stop listening to that radio. Its sound input is closed and
  nothing is recorded or transcribed until you press Resume; a transmission in progress is finished first.
  Remembered across restarts.
- **Clear log…** (on each radio card): clears that radio's log and stations heard from the screen. The day's
  log file and the recordings stay on the station PC (`data\logs`, `data\recordings`); nothing is deleted.
- **More room for the log on a phone:** tap the open tab (or **▴ Hide**) to fold the radio panel down to its tab
  bar; **▾ Show** or any tab brings it back (remembered per radio). Each net's header folds too (tap its title; open
  by default, remembered).
- **On the air** (under each radio's card, one transcript per radio): every transmission as it happens.
  Grey words appear while the station is still
  talking; the final text replaces them about 2–3 seconds after they unkey. Callsigns are buttons, Q-codes
  and prowords are underlined with their meaning below (untick **Explain radio terms** at the top to hide the
  meanings; on by default, remembered per browser), and ▶ plays the recording.
- **Who is talking** (first on each transmission): the speaker's callsign when it is known, in bold when the
  station said it on the air ("this is K7ABC", "K7ABC here", "de K7ABC", "…K7ABC, over/clear", a check-in such as
  "K7ABC, Springfield, no traffic", "W9XYZ, K7ABC, go ahead"). A callsign that is only being spoken TO is never taken
  for the speaker ("K7ABC, thank you", "you're in the log", "roger K7ABC", "go ahead K7ABC", "number 12, K7ABC").
  When nobody identified but the words are net control's (acknowledging, logging) and the schedule names net
  control (`ncs:`), it shows **≈ K7NET (net control)** in grey. Otherwise it shows nothing rather than guess.
  **Who?** lets you pick one of the callsigns in the transmission or type any callsign; your choice always
  wins (marked ✎), is kept in the day's log (`data\logs\<date>.jsonl`, survives restarts and the accurate
  pass) and goes into the ICS-309; **Automatic** forgets it. Measured on a fast HF check-in net (3.915 MHz,
  189 transmissions): 22 state their own station, 37 more are net control's words (shown when `ncs:` is in
  the schedule); the rest need a Who?.
- **What you can do now** (per radio): step-by-step help and example phrases when someone asks for a relay,
  calls emergency or priority traffic, opens a net, takes check-ins or passes formal traffic, plus a
  reminder when your own 10-minute identification is due.
- **Stations heard** (per radio, a tab beside On the air): one line per callsign (city, state, country · name, how
  often and when last heard); tap a line for the license, grid, zones and address (FCC, QRZ.com and the country file). Click any callsign for its card; the box
  at the top of the page looks up any callsign.
- **Net type** (a badge on each radio card): the kind of net being heard (ARES/RACES, SKYWARN, exercise, check-in, traffic, DX…),
  with a red banner if it sounds like a real emergency.
- **Nets** (on each radio card): the nets scheduled on that radio; the one running now is in green.
- **Nets…** (top of the page, and on each radio card for that radio's nets): add, edit or delete scheduled
  nets (name, radio, days, start time, minutes, frequency and mode, net control). Changes apply at once on
  every open browser, no restart. The schedule starts as the `schedule:` list in `config\station.yaml`; the
  first change made here is saved to `data\schedule.json`, which from then on is the schedule (station.yaml is
  never rewritten; delete schedule.json to start again from station.yaml).
  Per radio, **Listen only during scheduled nets**: the station resumes that radio 1 minute before each of its
  nets and pauses it 5 minutes after the net ends, so nothing between nets is recorded. The card says why it is
  paused ("Paused until Example County ARES net at 20:00" or "Paused by you"). Pause and Resume still work; the next
  net start or end takes over again. Optionally, **when a net starts, log the radio at the net's frequency and
  mode** (you still tune the radio yourself; the retune warning stays). Kept across restarts in `data\operator.json`.
- **Nets and conversations**: each radio's log is split into nets/conversations (a new one starts after 15
  quiet minutes or when the radio's frequency changes), each with a header: time span, frequency, how many
  transmissions and stations, and its own **Winlink messages…** button. That builds an **ICS-309**
  Communications Log (every transmission: time, from = who is talking as above — yours, then stated, then net
  control, "?" when unknown —, to, what was said; 30 rows a page) and an **ICS-213**
  summary (stations heard, traffic and relays) for that net only, addressed to every callsign that took part
  (callsigns no callbook confirms start unticked; **Uncheck all / Check all / Only callbook-confirmed**, and add
  any callsign). Edit, then **Put in Winlink Express Outbox**; Winlink Express sends them the next time you
  connect. Close Winlink Express first; the station refuses while it is open and backs up Winlink's message
  list before each write.
- **Accurate pass** (home network, optional), in two stages. As soon as each transmission's live text is done,
  the station sends its recording **with the ~25 s of the net before it** to the home server (Whisper large-v3) and
  puts a *provisional* accurate text beside the live text, a few seconds later. When the net has been quiet for
  5 minutes (`final_after_min`) it sends the **whole net** in 4-minute blocks (30 s of context each side) for the
  *final* accurate text, which replaces the provisional one. Whisper hears whole turns instead of 1–3 s pieces, so
  the final text gets far more right (see "How the accurate pass works" below). Anything missed (home server
  unreachable) is caught up 15 quiet minutes after the net; **Run now** on a net's header starts that at once. The header shows what it is doing: how many are done, a progress bar
  and about how long is left, then **✓ Accurate text for all N**. Corrected transmissions show the new text,
  with the live text a click away. The Winlink window says the same thing: send now with the live text, or
  wait; when the pass finishes it offers **Load the accurate text** (the forms then use it). If the home server is
  unreachable the header says so and the station tries again every 2 minutes. The upload key goes in
  `config\secrets.env` as `EMCOMM_STT_KEY=…` (kept across updates, never in git).
- **Test transcription…** (on each radio card): someone reads a fixed text over the air; **Score** shows how
  many words the live text and the accurate pass got wrong (0% = all right) and keeps a history, so a change
  to the radio, the cable or the settings can be judged by a number (`data\tests.jsonl`).
- **Help** (top right): opens the two-sided reference card (`/help`, from `station/static/card.html`) in a new tab.
  Print it double-sided on letter paper (flip on the long edge) and keep it by the radio.
- **Settings** (top right): your callsign, name and grid; which Winlink Express folder (found
  automatically: `C:\\RMS Express` and any other install) and which callsign account sends.

Which radios exist (and their sound inputs) and the first nets are set in `C:\ham-net-speech-to-text\config\station.yaml` (every option is explained in
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
| Repeaters / carrier that stays up between speakers: split at speech pauses (>= 0.8 s) | working (`split_pause_ms`, carrier-gate radios) |
| Sub-audible tone + mains hum removed before Whisper (high-pass 300 Hz on FM data-jack radios) | working (`highpass_hz`) |
| Whisper's noise phrases ("So.", "We'll be right back.") shown greyed as probable noise, left out of the ICS-309 | working |
| Icom IC-705 via Icom Remote Utility (voice gate) | working on the test PC; frequency read from the radio planned |
| Live transcription on the PC (engine picked per PC; W9XYZ-5: turbo on Intel graphics, final text 2.4 s after unkey for an 8.5 s transmission) | working |
| Words while someone is still talking (base.en, updated each second, replaced by the final text) | working |
| FCC callbook (1.7 M licenses, daily updates) + country lookup for any callsign | working |
| Near-miss callsign correction ('WB6BQW' -> WB6DQW heard in this net, 'K7XDG Waverly' -> K7XVG of Waverly); only when one reading clearly wins, what was heard kept (dotted underline + tooltip), live and accurate text, stations heard and Winlink use the corrected call; 15 min after a net ends it runs once more with the whole net known; uncertain ones show as "WV78? KV7RMV KB7XT" to click; expired licences are not ticked in the Winlink To: list | working (`hamkit/callfix.py`, `callfix: false` turns it off); 3.915 net: 8 corrections, none contradicted, most garbles left as heard |
| QRZ.com online lookups (grid, international names), cached for offline use | working (`hamkit.qrz set-login`) |
| Q-code / proword / abbreviation explanations | working (`config/glossary.yaml`) |
| Operator hints: relay, check-in, emergency, formal traffic, exercise, CQ/DX, 10-minute ID | working (`config/procedures.yaml`) |
| Net type recognition (emergency, exercise, ARES/RACES, SKYWARN, NTS, check-in, DX) | working, rules-based |
| Live dashboard in the browser (on the station PC or any PC/phone on the network) | working (`station/`, port 8091) |
| Winlink ICS-309 + ICS-213 into the Winlink Express Outbox, To: = everyone who took part | built; Outbox format matched against Winlink Express 1.8 messages (one and two recipients); first real write to be checked |
| Net schedule with radio, frequency and band; editor in the dashboard (Nets…) | working (`data/schedule.json`, seeded from `config/station.yaml`) |
| Listen only during scheduled nets (auto resume 1 min before, pause 5 min after; optional log at the net's frequency) | working, per radio |
| Accurate second pass on the home server (large-v3), progress + time left on each net and in the Winlink window | working (`accurate:` in station.yaml) |
| Accurate pass WITH CONTEXT: provisional text per transmission (heard with the net before it), final text for the whole net when it goes quiet (`POST /accurate/context`, `transcribe/context.py`) | built + measured, not deployed yet (synthetic FM nets: word errors 53 % → 38 %, callsigns found 11 % → 30 %; real 3.915 net: FCC-valid callsigns 39 → 62; see "How the accurate pass works"); older Mac services fall back to one at a time |
| Over-the-air transcription test with word-error score and history | working |
| Who is talking: stated callsign / net control by its words / operator's **Who?** (kept in the log, used by the ICS-309) | working (`station/speaker.py`) |
| Who is talking by VOICE (speaker embeddings, to fill in transmissions that name nobody) | measured, **not on**: on the 3.915 MHz LSB net same-voice and different-voice clips overlap almost completely (5 WeSpeaker models; first vs second half of one transmission: EER ~31 %, best match right 8–20 %). Code + `python -m tools.voice_eval` kept to re-measure on FM nets |

## How the accurate pass works

The station cuts a repeater net at every 0.8 s pause, so one "transmission" is often half a sentence, or one
speaker's tail and the next speaker's start, 1–3 s long. Whisper given such a piece on its own reads courtesy beeps
as "Thank you." and garbles callsigns it gets right inside a sentence. So the home server hears transmissions in
their place in the net (`transcribe/context.py`, `POST /accurate/context`):

1. **Stitch**: the transmissions go back on one timeline; pieces the station split at a pause are joined end to
   end, transmissions that were apart get 0.5 s of silence between them.
2. **Decode twice**: Whisper large-v3's own long-form decode walks the block 30 s at a time; separately, the block
   is cut into 22–29 s windows **only at real breaks or the longest pause**. No prompt and no "previous text"
   (both made Whisper copy earlier callsigns into later transmissions); temperature fallback on; Whisper's
   no-speech skip OFF (FM hiss made it drop whole 30 s windows of check-ins).
3. **Guards** per segment: loops, prompt echoes, and runs of "! ! !" are dropped.
4. **Merge** the two decodes word by word by confidence (their window edges fall in different places) and give
   each word back to the transmission it was spoken in (by time; at a pause split the boundary sits 0.4 s early
   because Whisper's word times run early).
5. Per transmission: text, word times, mean word confidence and flags. Empty, a lone "Thank you."-style phrase
   with low confidence, or confidence under 0.3 → the station keeps the live text (`accurate_echo`).

Stages: **provisional** = each transmission with the ~25 s before it, sent as it ends (median 0.8 s on the Mac
Studio, ~3 s for a silent clip); **final** = the whole net in 240 s blocks + 30 s of context each side, when the net
has been quiet 5 minutes (~11x real time: a 1-hour net in ~6 minutes). A late provisional never overwrites a final.

Measured 2026-11-03 (`tools/accbench`: synthetic nets with known text — macOS voices reading real regional
callsigns phonetically, through a simulated FM repeater/data jack or SSB channel matched to the 11-02 recordings,
cut by the station's own segmenter; word error rate, lower is better):

| | FM (4 nets) | SSB moderate (2) | SSB hard (2) | callsigns found (all 8) |
|---|---|---|---|---|
| live text (turbo + prompt) | 55.0 % | 50.7 % | 90.2 % | 12.8 % |
| one clip at a time (the accurate pass until now) | 53.2 % | 51.8 % | 94.2 % | 10.6 % |
| provisional (25 s before) | 43.4 % | 34.5 % | 93.9 % | 19.0 % |
| **final (long + windows, merged)** | **38.2 %** | **29.0 %** | 93.0 % | **25.6 %** |

Real 11-02 nets (no reference text; FCC-valid callsigns found, live text kept where set aside): 3.915 LSB 40
(live) / 39 (old accurate) / **62** (final); 442.100 FM 81 / 73 / **86**; 3.985 0 / 1 / 0. What did not help:
a second or third shifted tiling (55.4 % vs 55.3 %), the previous text as a prompt or as conditioning (fewer
errors overall but callsigns copied from earlier turns), temperature fallback on short clips, dropping segments on
Whisper's no-speech / log-probability numbers, large-v3-turbo (53.4 % vs 52.7 % long-form), Parakeet TDT 0.6B
(fast, but left out much of the speech on the real FM recordings; not scored on the synthetic set). Hard SSB (−4…4 dB) stays unreadable for every method. The real test is the
over-the-air **Test transcription** score.

Re-run: `python tools/accbench/gen.py OUT fm 1` makes a net; `python -m tools.accbench.run final OUT` decodes it;
`python -m tools.accbench.score OUT --methods=live,clip,prov,final` scores it.

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
Details: `docs/ARCHITECTURE.md`, deploy: `deploy/my-server/`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r transcribe/requirements.txt -r analyze/requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
Design and history: `docs/WEBAPP-DESIGN.md`, `docs/ARCHITECTURE.md`, `docs/HANDOFF.md`,
`continuation-prompt.md`.
