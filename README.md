# HamNet Station — ham net speech-to-text

Live speech-to-text for **amateur radio voice nets**, on a Windows PC connected to one or more radios.

> **An independent volunteer project** (MIT License), offered freely to any amateur radio club, ARES (Amateur Radio Emergency Service) group or EOC (Emergency Operations Center) that can use it. A companion to the [Church of Jesus Christ EMCOMM forms](https://github.com/travislhilton/church-emcomm-wnlk-forms): the station writes the net's ICS (Incident Command System) 309 and 213 messages for Winlink, and can read an EOC's situation report out on a voice net. Machine transcription: the operator reviews everything before it is sent.

HamNet Station listens to your radio and writes down every transmission a few seconds after the station unkeys. It shows who is talking, looks up every callsign in the FCC (Federal Communications Commission) database, explains Q-codes and prowords, coaches a newer operator through check-ins, relays and emergency traffic, and keeps each net's log, minutes and history. After the net it builds the **ICS-309** communications log and **ICS-213** summary for Winlink Express. It works with **no internet** once installed; a bigger home computer, when there is one, can re-read the net more accurately.

## Find your role

| You are | Start here |
|---|---|
| Setting up a station PC for the first time | [Install](#install) — then [After installing — what to do](#after-installing--what-to-do) |
| An operator or net control logging a net | [Running a net](#running-a-net) and the [Quick Reference Card](#quick-reference-card) |
| Sending the net's log to an EOC or your ARES group | [After the net: Winlink messages](#after-the-net-winlink-messages) |
| Reading an EOC's situation report out on a voice net | [Incident read-out](#incident-read-out-situation-report-on-a-voice-net) |
| Connecting a radio (handheld, mobile, HF rig) | [Connecting a radio](#connecting-a-radio) |
| An instructor giving a class | [Quick Reference Card](#quick-reference-card) — print one per student |
| Something looks wrong | [When something looks wrong](#when-something-looks-wrong) |
| Every feature in detail | [The manual](docs/MANUAL.md) |
| A developer | [Contributing](#contributing--developing-this-project) and [How it works inside](docs/TECHNICAL.md) |

## Which computer can I use?

| What you are doing | What you need |
|---|---|
| **Running the station** (it records and transcribes the radio) | **A Windows 10 or 11 PC**, 64-bit, about 8 GB free; a laptop works |
| Watching the log, fixing a line, reading the minutes | Any phone, tablet or computer with a browser, on the same Wi-Fi or Tailscale |
| Sending the net's messages by Winlink | The station PC, with Winlink Express |

**Why Windows:** the station runs beside Winlink Express and VARA (the radio modem Winlink uses), which run only on Windows. Any other device joins by browser.

## Download

| | Best for | Link |
|---|---|---|
| **ZIP package** (the installer is inside: `install\windows\install.cmd`) | Every station PC — the first install and every update | **[Download the ZIP](https://github.com/travislhilton/ham-net-speech-to-text/archive/refs/heads/master.zip)** |
| **Quick Reference Card** (two sides, US Letter) | Keeping by the radio; handing out at a class | **[Open the PDF](https://cdn.jsdelivr.net/gh/travislhilton/ham-net-speech-to-text@master/docs/HamNet_Station_Quick_Reference_Card.pdf)** |
| Sidekick: **up to 3 questions** worked on at once, answered in the order asked; outside a net a ready answer is **given at a pause** (3.5 s quiet, a turn-over, or when asked: "This is W9XYZ Sidekick, for K7ABC: …"), with the disclosure when it is the first transmission; in a net it still announces and waits for net control. "What did you find?" / "give us your answer" ask for it | built 2026-11-12 |
| Sidekick **Send** (a third position on a radio's Sidekick switch, offered only when that radio's Transmit switch is on; off by default): a draft that would speak is said on the air after a **3 s countdown** ("Sending in 3… Stop (Esc)"), only while the control operator is **at the station** (keyboard or mouse on the dashboard in the last 3 min), with listen-before-talk (carrier or voice, or anything new heard, cancels it), the frequency known, PTT through the radio's transmitter watchdog. **Esc, Stop or any click drops PTT at once.** The chosen Grok voice, else this PC's Windows voice offline. The ID clock, disclosure, check-in and prepared answers move on only when really sent. Logged to `data/agent_sends.jsonl` (`station/sender.py`, `tests/test_send.py`) | built 2026-11-12, not yet keyed on a real radio |
| **Sidekick card** (two sides, US Letter) | Setting up Sidekick and reading it on a net | **[Open the PDF](https://cdn.jsdelivr.net/gh/travislhilton/ham-net-speech-to-text@master/docs/HamNet_Sidekick_Quick_Reference_Card.pdf)** |

The same ZIP is on this page under the green **Code** button → **Download ZIP**.

---

## Install

1. **Download** the [ZIP package](https://github.com/travislhilton/ham-net-speech-to-text/archive/refs/heads/master.zip) and save it anywhere.
2. **Unblock** — right-click the ZIP → Properties → tick **Unblock** → OK *(skip it if there is no Unblock box)*.
3. **Extract** — right-click the ZIP → **Extract All** → Extract.
4. **Run the installer** — in the extracted folder open `install` → `windows` and double-click **`install.cmd`**. Press **Yes** when Windows asks for administrator rights. If Windows shows "Windows protected your PC", click **More info** → **Run anyway**.
5. **Type your callsign** when asked. Press **Enter** to skip the QRZ.com question if you don't use QRZ (you can add it later).
6. **Keep the PC plugged in and leave it alone while it measures.** The installer measures how fast this PC transcribes, with the screen held on (a laptop with its screen off runs several times slower and would get a slower setup).
7. **Choose when the station may change your power settings** (it asks once; press **Enter** for the recommended choice). While it transcribes, the station keeps the screen on and switches to its own **HamNet Station** power plan at Best performance, then puts your own plan back. Settings → *Full speed and screen on* changes it later.
8. **Wait for `== Done`.** The first install downloads about 4 GB (Python packages, Whisper speech models, the FCC license database) and takes 10–30 minutes. It ends with **This PC**: anything in your power settings that would slow the station (on battery, power-saving mode, hibernate, an old graphics driver, a quiet fan profile), each with its fix. Then it lists the sound inputs this PC can record from: note your radio's.

**Internet is needed for the first install only.** The station itself works with none.

**To update:** download the ZIP again and run `install.cmd` again. Everything you set is kept: radios, net schedule,
settings, local names, learned fixes, keys, QRZ login, logs and history (they live in `data\` and the station's own
config files, which an update never overwrites). The earlier speed measurement is kept too; `install.cmd -Remeasure`
or **Settings → This PC → Measure this PC again** measures again (after new graphics drivers or a new PC).

### What the installer does

`install/windows/install.ps1`, safe to re-run. Nothing needs to be installed beforehand:
- checks the PC first (64-bit Windows 10/11, about 8 GB free, internet for the first install) and says what is missing;
- installs the Microsoft Visual C++ runtime if it is missing (OpenVINO, PyTorch and faster-whisper need it);
- installs Python 3.12 for all users (winget, or straight from python.org when winget is missing) and a private
  environment in `C:\ham-net-speech-to-text\.venv` with every Python package the station uses;
- downloads four Whisper models in Intel OpenVINO format (three for the live text, Whisper large-v3 for the
  accurate pass; plus faster-whisper large-v3 on a PC with an NVIDIA card) and **benchmarks this PC** to choose the
  live engine (`tools/pick_engine.py` → `config/engine.yaml`): Intel graphics, NVIDIA (CUDA) or CPU;
- **measures what this PC can do for the accurate pass on its own** (`tools/pick_accurate.py` → `accurate:` in
  `config/engine.yaml`): each accurate model on each device (Intel GPU, CPU; NVIDIA; not the NPU, see below), then decides *both
  stages* (fast enough to follow every transmission), *final text only* (after each net) or *none*;
- downloads the FCC amateur license database and the world country file, and schedules a daily
  update (task "HamNet Callbook Update", 04:15, only when online);
- asks (once, optional) for a QRZ.com login: Enter skips it. It checks the login and says in plain words what the
  account gives (full records need QRZ's "XML Logbook Data" subscription; without it only names and countries; US
  callsigns are covered by the FCC database either way). The password goes to Windows Credential Manager;
- reports the graphics card and driver (Intel/NVIDIA graphics are used only with a current driver);
- keeps your settings (`config/station.yaml`, `config/engine.yaml`, `config/local.yaml`) across updates;
- starts the station at every sign-in (task "HamNet Station", which also brings it back within 2 minutes if it
  ever stops), puts a **HamNet Station** shortcut on the
  desktop, and opens port 8091 to your home network and Tailscale only;
- lists the audio inputs it can record from.

**On a laptop:** keep it plugged in, set the maker's performance profile to **Performance** (ASUS: MyASUS → Fan
Profile), turn off *Turn off my screen when I leave* (Settings → System → Power & battery → Presence sensing) and never
let it hibernate on mains power. Laptops with Modern Standby slow to about a fifth of their speed once the screen is
dark, so while the station transcribes it holds the screen on and switches to its own **HamNet Station** power plan,
then puts yours back. **Settings → This PC** shows what it changes and anything that slows this PC. The details:
[On a laptop](docs/MANUAL.md#on-a-laptop).

---

## After installing — what to do

1. **Open the dashboard:** double-click **HamNet Station** on the desktop (or browse to `http://localhost:8091/`).
   The station starts by itself whenever you sign in to Windows.
2. **Add your radio:** **Radios…** at the top → **+ Add a radio** → a name, its **sound input**, where its audio comes
   from (*speaker / earphone jack or SSB*, or *FM data jack*), mode and frequency → **Save and restart the station**.
   An Icom IC-705: also choose its **CAT port** (computer control), so the station reads the frequency and tunes the
   radio for its nets. Details: [Connecting a radio](#connecting-a-radio).
3. **Set the level:** **Set level…** on the radio's card walks you through the radio's volume and squelch until the
   station hears it cleanly.
4. **Add your nets:** **Nets** → **+ Add a net**: name, radio, start, length, frequency, net control and how it
   repeats (every Tuesday, 1st and 3rd Sunday…). Five minutes before each one a yellow alert tells you what to set.
   A transmission counts as the net's only when the radio is on the net's frequency.
5. **Open it on your phone** (optional): **Open on phone** at the top → point the phone's camera at the code (same
   Wi-Fi as the PC, or Tailscale).
6. **Settings** (optional): your name, grid and Winlink Express account (for the Winlink messages), a QRZ.com login,
   your area's local names, and an xAI API key for **Sidekick**, a station assistant that drafts what your station
   could say on the net and never transmits.
7. **Print the [Quick Reference Card](#quick-reference-card)** and keep it by the radio.

### After the net: Winlink messages

When net control closes the net (or you press **End net**) recording stops and the accurate pass re-reads the net.
Press **Winlink messages…** on the net's header: you get an **ICS-309** communications log and an **ICS-213**
summary, titled with the net's name. Check the To: list (stations the callbook can't confirm start unticked), read
and edit the messages, **close Winlink Express**, press **Put in Winlink Express Outbox**, then open Winlink Express
and connect. They go out with your next session.

## Running a net

**The screen.** The navy bar at the top is the station: your call, the net on the air, the clock (local and UTC),
**● Live text** (the speech engine is ready) and **● Home server** (where the better second reading is made, see
below), **Search everything**, and the buttons **Nets**, **Radios…**, **Settings**, **Open on phone**,
**Read-out**, **☾ Night** and **Help** (the reference card). Below it, one column per radio: its nets today, its
card (frequency, Signal and SNR meters, waterfall, **Change**, **Pause**, **Set level…**), and its log. On a phone
the radios are tabs.

**Before the net.** Five minutes before, a yellow alert says what to set ("set the FTM-150R to 146.940 MHz FM").
Tune the radio and press **Radio is set** (a radio with CAT tunes itself). A transmission counts as the net's only
when the radio is on the net's frequency, so if you retune, press **Change** on the card.

**During the net.** Each transmission appears a few seconds after the station unkeys: grey words while it is still
talking, then the text. The callsign at the start is **who is talking** (bold = they said it; ≈ grey = probably net
control; blank = not known: press **Who?**). Tap any callsign for its license card. **✎ Fix** corrects a line (the
minutes, search and Winlink messages use your text). **Coach** (a tab on the radio's card) says what to do when
someone asks for a relay or calls emergency traffic; a red **Urgent** box keeps anything urgent in view. **Stations
heard** lists every call with name and town. The **Net board** tab on each radio shows who is in (numbered), who to ask again, who has traffic,
regulars not heard yet, and when net control last said its call.

**After the net.** When net control closes the net (or you press **End net**), the station re-reads the whole net
for better text and drafts the **minutes** (attendance, announcements, action items, traffic). Then
[After the net: Winlink messages](#after-the-net-winlink-messages). Open the **Net board** tab (or the minutes' Attendance) and **review the check-ins**: ▶ listen, ✓ right,
✎ the right call, ✗ not in the net. About half the calls the station hears only once are mis-heard; your marks fix
the minutes and the Winlink messages.

**At night.** **☾ Night** (top right) turns the whole screen red on black to keep your night vision; press it again
for normal colours. **Settings → Display** chooses Automatic / Light / Dark / Night and a 24-hour or 12-hour clock
(per browser). The colours match the companion EMCOMM forms' EOC Message Center.

Every feature in detail: [the manual](docs/MANUAL.md).

## The home server (optional)

A station works on its own. A **home server** (a Mac with Apple silicon on the same network or Tailscale, running
`transcribe/`) adds a better second reading of each transmission with a larger Whisper model: the **accurate pass**.
The top bar says where it is made:

| Top bar | What it means | What you do |
|---|---|---|
| **● Home server** (green) | The home server answers; each transmission gets better text within seconds | Nothing |
| **● Accurate on this PC** (amber) | The home server is not reachable; this PC makes the better text itself (after each net if it is not fast enough to follow every transmission) | Nothing; it goes back to the home server by itself |
| **● Home server offline** (amber) | The home server is not reachable and this PC cannot do the accurate pass: it waits | Nothing for the net; the text comes when the server is back |
| (none) | No home server set up, and this PC does not do it | Nothing: the live text is what you get |

**The live text never depends on the home server**: it is made on the station PC and carries on whether the server
is there or not. The station checks the server every minute; when it comes back, whatever waited is sent at once.
If the server goes away in the middle of a net, the rest of that net's work moves to this PC (or waits) without
holding up the live text. The Winlink messages use the best text there is: your corrections, then the accurate pass,
then the live text. Setting one up: [Home-network server](docs/TECHNICAL.md#home-network-server-optional).


## Quick Reference Card

Two sides on one sheet: **Install and Get Started** on the front, **Running a Net** on the back. Same layout as the
Church of Jesus Christ EMCOMM quick reference cards. Print it double-sided ("flip on long edge"), laminate it, and
keep it by the radio. Works in black and white.

**[Open the PDF](https://cdn.jsdelivr.net/gh/travislhilton/ham-net-speech-to-text@master/docs/HamNet_Station_Quick_Reference_Card.pdf)** ·
[view it on GitHub](https://github.com/travislhilton/ham-net-speech-to-text/blob/master/docs/HamNet_Station_Quick_Reference_Card.pdf) ·
on the station: **Help** (top right of the dashboard) opens the same card; the browser's Print prints it.

**Sidekick card** (two sides: setting up Sidekick, and Sidekick on a net):
**[Open the PDF](https://cdn.jsdelivr.net/gh/travislhilton/ham-net-speech-to-text@master/docs/HamNet_Sidekick_Quick_Reference_Card.pdf)** ·
on the station: **Settings → Station agent → Sidekick card**, or **Card** on Sidekick's card on a radio.

The card's QR code opens this download page. A club handing out its own copy sets `download_url:` in
`config\station.yaml` and the card the station prints carries that address instead. To rebuild the PDF after
editing `station/static/card.html`: `python -m tools.build_ref_card` (needs Google Chrome; it fails when a side no
longer fits one page).

## When something looks wrong

| You see | Do this |
|---|---|
| No text at all | Is the radio **Paused**? Does the Signal meter move? Does the squelch open on traffic? Run **Set level…** on its card |
| The same words for every transmission, or "The." over and over | The live engine is stuck: the station checks and reloads it while a dashboard is open. If it carries on, run **Test transcription…** on the card, or restart the station (sign out and back in) |
| "Its sound input … is not there" on a radio's card | The audio cable is unplugged, or (IC-705 on Wi-Fi) Icom Remote Utility is not running. **Choose its input…** picks another |
| The meter is red / shows nothing | Too loud: turn the radio's volume down. Nothing: check the cable and the radio's input in **Radios…** |
| The wrong net name on the log | Is the frequency on the radio's card what the radio is on? **Change** it |
| **● Home server offline** at the top | Nothing for the net: the live text carries on. See [The home server](#the-home-server-optional) |
| **🔒 Close Winlink Express first** in Winlink messages | Close Winlink Express; the button turns green (**✓ Put in Winlink Express Outbox**) within 2 seconds |
| The page won't open | Sign in to Windows on the station PC (the station starts at sign-in). A phone: same Wi-Fi or Tailscale |
| A slow-transcription warning | Plug the laptop in, wake the screen, check **Settings → This PC** |


## Connecting a radio

Each radio needs its receive audio on a PC sound input of its own (never the one a digital mode
such as VARA FM uses). Give that input a clear name in Windows (Windows 11: Settings → System → Sound → the input →
Rename; or `Win+R` → `mmsys.cpl` → Recording → the device → Properties), e.g. `NET-REC`: two identical adapters (one
for the net, one for Winlink) can then be told apart.

**Radios…** (top of the dashboard) adds, replaces or removes a radio: a name, its sound input (chosen from the inputs
plugged in), where its audio comes from (*speaker / earphone jack or SSB* = voice gate, squelch closed; *FM data
jack* = carrier gate, hiss always on), mode and frequency. Saving restarts the station (about 15 s, the page
reconnects). Once saved there, the list lives in `data\radios.json` and `station.yaml`'s `radios:` is no longer read
(delete the file to go back); meter and segmenting settings from `station.yaml` are kept per radio id. Inputs can change
under a running station (renamed in Windows Sound settings, re-plugged, a driver update that makes new ones): the
list in Radios… is always read fresh, a radio whose input comes back reconnects by itself within about a minute, and
a radio whose input is gone says what Windows did with it and offers **Choose its input…**. A radio with
nets cannot be removed until its nets move to another radio. Inputs are matched exact name first, so a radio on
"Digirig Lite" never opens "Digirig Lite NET-REC". Then use **Set level…** on its card.

**Transmit (PTT), set up but switched off.** Each radio in **Radios…** has a **Transmit (PTT)** section: how the
station would key the radio — **Sound card GPIO** (what VARA FM calls *RA-Board*: Digirig Lite, AIOC, CM108/CM119
interfaces, pin 3), or a serial **RTS / DTR** line on its own COM port (never the CAT port) — the **Audio out** the
transmitted voice goes to, and a **time limit** (5-180 s, default 60) after which the transmitter is let go whatever
happens. **Allow transmitting on this radio** is OFF by default; while it is off nothing can key the radio. Nothing in
the station transmits yet (Sidekick only drafts): this is the setup a future *send* will use. A voice transmission
needs a control operator at the station (FCC 97.109) and your call sign (97.119). The GPIO is reached through
`hidapi` (installed with the station).

| Radio | Interface | Notes |
|---|---|---|
| A handheld (Baofeng, Wouxun KG-UV9PX+, …) | speaker/earphone jack → Digirig Lite or Mobile (USB) | Audio from: *speaker / earphone jack* (voice gate). The jack is very loud: Set level… step 1 finds the cleanest volume knob position. On all day: run it from a charger or battery eliminator — a flat battery shows as **the radio seems to be OFF** on its card. |
| Yaesu FTM-150R (VHF/UHF) | DATA jack (10-pin mini-DIN) → Masters DRA-50M (USB) | Set 45 DATA BAND = A-BAND FIX, tune band A to the net; leave 41 DUAL RECEIVE MODE (a priority scan) OFF. The DATA jack is unsquelched, so the recorder's **carrier gate** detects transmissions from the drop in hiss. Step by step: `docs/FTM-150R-SETUP.txt`. |
| Icom IC-705 (HF/VHF/UHF, Wi-Fi) | Icom **Remote Utility** (connected to the radio) → its virtual sound input `IC-705 (Icom Virtual Audio Driver)` | In `station.yaml`: `device: IC-705`, `gate: voice` (SSB has no carrier to detect). Remote Utility must be running and connected. **CAT** (optional, recommended): in **Radios…** choose the radio's **CAT port** — Remote Utility's virtual CI-V COM port on Wi-Fi, the radio's first COM port on USB (CI-V address A4). The card then shows the frequency read from the radio, **Change** tunes the radio, and each of its scheduled nets tunes it (any band) and puts it back after. |

## Incident read-out (situation report on a voice net)

The station can read an EOC's situation report on a voice net — from the companion EOC Message Center
(`GET http://<eoc>:8080/api/readout`) or a file carried by hand. The shared format is `contracts/emcomm-readout/`
(`emcomm-readout/1`, JSON Schema + example fixtures). Open **Read-out** in the dashboard header (or `/readout` on a phone):

1. **Get from the EOC** or **Load a file**. A document that carries injuries, deaths, missionary status or details
   about people is refused, not filtered: those are never read on an open frequency.
2. The facts and a voice script written by a fixed template (no AI): sections of about 30 seconds, numbers in words,
   the call sign at the end of each section, "This is an exercise" around each section during a drill.
3. **Approve**: by the Stake President or his delegate (stake and satellite EOCs) or the Area Authority or his
   delegate (area EOC). The approval binds to the report's fingerprint; a newer report with different facts needs a
   new approval and shows what changed.
4. **Read on the air**: a large-type card, one section at a time. The operator keys the radio and reads in their own
   voice; the station never keys a transmitter. Reading it is logged (`data/readouts.jsonl`) and noted in the minutes
   of the net that is on.

## Requirements

| Requirement | Notes |
|---|---|
| **Windows 10 or 11, 64-bit** | The station PC. About 8 GB free; a laptop works (keep it plugged in) |
| **A sound input per radio** | A USB sound card or interface (Digirig, SignaLink, a data-jack cable) — never the one VARA or Winlink uses. An IC-705 on Wi-Fi uses Icom Remote Utility's virtual sound input |
| **Internet, the first time** | About 4 GB for the install. Afterwards the station works with none |
| **Winlink Express** | Only to send the net's messages by Winlink · [winlink.org](https://winlink.org) |
| **Intel or NVIDIA graphics** (optional) | Faster text with a current driver; the CPU works too. The installer measures this PC and picks |

## Your own settings stay on your PC

Everything personal is kept in files the repository never contains (they are git-ignored and the installer
never overwrites them): your callsign, name and grid and the radios in `config\station.yaml`; your area, repeaters
and roster in `config\local.yaml`; the accurate-pass server's address in `station.yaml` (`accurate: urls:`) and its
key in `config\secrets.env`; your own **xAI API key** for Sidekick, its voice and Grok speech-to-text (optional: **Settings → Station agent**,
paste it and **Save key**; **Test** asks xAI whether it works; the page only ever shows "set …abcd"; it is kept in
`config\secrets.env` as `XAI_API_KEY=…`, readable only by the station's user, SYSTEM and Administrators — create one at
console.x.ai → API Keys after adding a little credit under Billing; optionally an **Anthropic API key** the same way,
to compare Claude Haiku with Grok); the QRZ.com password in Windows Credential Manager; SSH public keys for remote
management in `deploy\windows\ssh-keys.txt`. The server keeps its settings in `~/.config/emcomm-stt/env`.

## How long recordings and net history are kept

**6 weeks by default; Settings → Keep recordings and net history for … weeks** (1–104; `keep_weeks:` in
`station.yaml` is the starting value, the dashboard's choice wins). A busy two-radio station records about 130 MB a
day. Every 6 hours (and 10 minutes after the station starts; never while a radio is receiving or transcribing) the
station deletes whole days older than that (`station/retention.py`): the recordings (`data\recordings\<day>`), the
day logs (`data\logs`), those days' nets, transmissions, who took part, minutes and items in the history database
(then compacted), voiceprints, the station agent's drafts and Grok comparisons. A net that started before the first day
kept goes whole. Shortening the time asks first and deletes about a minute after you save. It also keeps two
caches in check: a removed radio's leftover raw audio (over a day old) and OpenVINO's compiled models (over 3 GB, the
least recently used over 2 days old go; every re-measure adds about 1.8 GB), and cuts `data\station.log` to its last
2 MB at start once it passes 10 MB. **Never deleted:** settings, radios, the net schedule, local names, learned
fixes, keys and the callbook. Settings → This PC shows the disk used, the oldest day kept and the last cleanup.
`python -m station.retention --dry-run` lists what would go.

## The full manual

[docs/MANUAL.md](docs/MANUAL.md): every feature (the log, Who?, corrections, local names, search, the nets window,
minutes, Winlink, the end of a net, HF scan, Sidekick, the urgent board…). [docs/TECHNICAL.md](docs/TECHNICAL.md):
segmenting, the accurate pass, the home server.

## Support, feedback and bug reports

Found a problem, or have an idea? Anyone with a GitHub account can **[open an issue](https://github.com/travislhilton/ham-net-speech-to-text/issues/new)**: say what you did, what you saw, the radio and interface, and the time of the transmission. Please don't post recordings of other operators' transmissions or their personal details.

## Contributing — developing this project

```bash
python3 -m venv .venv && .venv/bin/pip install -r transcribe/requirements.txt -r analyze/requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
Design: `docs/ARCHITECTURE.md`, including which settings live in which local config file. Your callsign,
server addresses, keys and logins go only in those files (`config/station.yaml`, `config/local.yaml`,
`config/secrets.env`, `capture/config.yaml`, `~/.config/emcomm-stt/env`); git ignores them.

## Status

| Piece | State |
|---|---|
| Live signal + SNR meters and waterfall per radio; several radios at once | working |
| A silent radio is flagged on its card and in station.log: a handheld switched off / asleep / flat (input dead flat ≥ 3 dB under its own learned squelch-closed floor for 2 min), a data jack with no hiss (2 min), or a net on the air with nothing heard for 10 min (`station/silence.py`, per radio `silence:`) | working: replayed on the hour a handheld died (flagged 2 min after, cleared when switched on; no false alarm on the squelch-closed gaps) |
| **Morse code (CW)** on any radio, any frequency: a keyed tone in the continuous audio (not only gated transmissions) is decoded and shown in the transcript as **CW · Morse code** with its speed and tone, the dots and dashes as heard, then the text (→ CQ DE W9XYZ K); prosigns as <AR>, <SK>, <BT>, <KN>; ▶ plays it. Heard during a voice transmission it is added to that transmission; alone it is a row of its own (logged, searchable, callsigns picked out; never sent to the accurate pass). Counted as Morse only when the timing sits on Morse's 1:3:7 grid, the tone is steady and at least 3 characters decode (`station/morse.py`; `morse: false` per radio turns it off) | working on synthetic CW (5–50 wpm, 0 dB SNR, 15 % hand-keyed timing jitter, prosigns); no false detections in 76 min of real and synthetic voice (460 recordings), a steady carrier, courtesy beeps, DTMF or sub-audible tones; first on-air CW to be confirmed |
| Radios… on the dashboard: add / replace / remove a radio, sound input chosen from those plugged in (exact name first), station restarts itself | built 2026-11-05 |
| Inputs that change under a running station: fresh input list, self-reconnect when an input comes back, "Choose its input…" on a radio's error, and what Windows did with a missing input (a driver update's new input on the same USB port) | built 2026-11-07 |
| Set level… per radio: noise and voice against a target, Windows recording level read/set, Adjust automatically | working (handheld's speaker jack into a Digirig: -13 dBFS clipping → -24 dBFS in one step) |
| Recording per transmission (carrier gate for FM data jacks; voice gate otherwise) | working, tested on the FTM-150R |
| Weak-signal repetition guard (Whisper loops) | working |
| Repeaters / carrier that stays up between speakers: split at speech pauses (>= 0.8 s) and carrier dips, cut in the middle of the pause, a fragment ("Over.", the end of a call sign) kept with its neighbour, a voiceless repeater tail never its own transmission | working (`split_pause_ms`, `dip_split_ms`, carrier-gate radios; see "Segmenting"); 2026-11-03 FM net replayed: 140 → 105 transmissions, blank ones 31 → 3, words lost in dropped pieces 79 → 0 |
| **Sidekick** (the station agent, Draft mode): operator-named (call-like names refused), two speeds (quick model per turn; a thinking model for a question, announced "response ready" when net control gives the floor, read word for word when told to go ahead), offline rules (check-in, acknowledge, announce/read a prepared answer), memory across a restart, local names in its context, FCC Part 97 checks in code | built + unit-tested 2026-11-10; offline rules replayed on a real 280-transmission FM net (one check-in, at "next station, please" in open check-ins; silent for city/EOC/county calls); the models on real nets: see the replay |
| Sidekick's voice: xAI TTS audition of every voice, chosen in Settings, ▶ Hear it on drafts (browser only, never a radio) | built + unit-tested 2026-11-10 |
| Grok speech-to-text compared live on chosen radios (never queued; skipped offline/busy/over the minutes), Compare today, `tools/grok_compare.py` | built + unit-tested 2026-11-10 |
| Voice gate on squelched FM: the pre-roll reaches back to where the squelch opened (up to 2.5 s; the 11-10 roll call lost the first letters of 60% of its calls to a late VAD) | built + unit-tested (`squelch_db`), 2026-11-10; measured on the next squelched-FM net |
| Far call one rhyming letter from a licence within 50 km: not counted, the near ones offered (never moved); a net's own **Reach (km)**; PO-box ZIPs placed by region (far only); a net edit keeps its repeater offset/tone | working (`station/consensus.py`, `hamkit/zipgeo.py`), 2026-11-10 |
| HF / voice gate: 600 ms pre-roll; a short piece waits 0.6 s after the hangover for its continuation (a call sign spelled with gaps, a kerchunk before the words) | built + unit-tested (`merge_gap_ms`, `merge_max_s`, `preroll_ms`); not yet measured on a live HF net |
| Sub-audible tone + mains hum removed before Whisper (high-pass 300 Hz on FM data-jack radios) | working (`highpass_hz`) |
| Whisper's noise phrases ("So.", "We'll be right back.") shown greyed as probable noise, left out of the ICS-309 | working |
| Icom IC-705 via Icom Remote Utility (voice gate) | working on the test PC |
| **Transmit (PTT) setup** per radio (Radios… → Transmit; `tx: {ptt: cm108, hid, pin: 3, out, max_s, enabled: false}`): sound-card GPIO (VARA's RA-Board) or serial RTS/DTR, audio out, time limit with a watchdog; refused unless **Allow transmitting** is on (off by default); nothing calls it yet (`station/ptt.py`) | built + tested with simulated interfaces; no key-up on a radio yet |
| **CAT control (Icom CI-V)** per radio (`cat: {port: COM11, civ: A4}`, or Radios… → CAT port): the frequency and mode are read from the radio every 5 s (card + every transmission); **Change** tunes the radio; a scheduled net on that radio — VHF/UHF or HF — tunes it at the net's start (frequency, mode, tone squelch off; the net's repeater offset and tone when it gives them) and puts back what it was on when the net ends, unless the operator retuned during the net (kept across a restart). Never transmits: only reads and these settings can be sent (`station/cat.py`) | built + tested on a simulated IC-705 (CI-V echo, HF/VHF/UHF, restart mid-net, missing port); first test on the radio pending |
| Live transcription on the PC (engine picked per PC; W9XYZ-5: turbo on Intel graphics, final text 2.4 s after unkey for an 8.5 s transmission) | working |
| Words while someone is still talking (base.en, updated each second, replaced by the final text) | working |
| FCC callbook (1.7 M licenses, daily updates) + country lookup for any callsign | working |
| Callsigns spelled phonetically ("Kilo X-ray 7 Fox, Quebec, Zulu" → KX7FQZ; "Fox" for Foxtrot); Whisper's usual mishearings ("Helo", "Sheila", "Gila", "Cilo" for Kilo, "Indy" for India, "Austin" for Oscar, "Vicky" for Victor, "Yanky") count only when the FCC database or roster knows the call | working; a call split between written letters and phonetics ("W9X, Yankee, Zulu…") is not joined yet |
| Net-wide callsign agreement: variants of one station merged by FCC licence, first name, town, earlier check-ins; clipped and unlisted calls found only with a name/town/history; nothing supported = kept, *unverified* | working (`station/consensus.py`, `tools/consensus.py`, `consensus: false`); 11-03 FM net (140 transmissions, FCC on the dev Mac, no history yet): 11 readings moved, 5 calls found in the text, none contradicted by the transcript, the ambiguous ones left as heard. First live night (3 nets): 41 moves, several over-reached (two letters on a name heard in the reply, one name used twice, merging a call heard as its own station); the rules above now keep 25 of them and drop 16 (dry run, same day); clipped calls back under the one-licence rule: 1 of 4 completed (the other 3 have two in-state licences each, left as heard) |
| VHF/UHF callsign distance (Census ZIP centres): clipped calls to the clearly nearest licence, in-range scoring instead of the state, calls beyond 300 km (Settings → VHF/UHF reach) that nothing backs not counted | working (`hamkit/zipgeo.py`); 11-03 nets: every station heard 3+ times was within 50 km, 32 of 33 licences beyond 250 km were heard once (misreadings) |
| **VHF/UHF path analysis** over real terrain (for Sidekick to call as a tool when planning a net or Winlink links, never guessing terrain): ground sampled every 100 m along the path (USGS 3DEP, or Open-Meteo's 90 m worldwide data; cached in the callbook folder; no data = an error, never flat ground), k = 4/3 earth, line of sight and Fresnel clearance, the worst obstruction, free-space + knife-edge/Deygout diffraction loss (ITU-R P.526), link budget and fade margin for FM voice / VARA FM / 1200-baud packet, a verdict in plain words; every pair of sites, the best relay site, the mast height that clears the path (`hamkit/pathprofile.py`, `python -m hamkit.pathprofile`) | built + unit-tested on synthetic terrain 2026-11-12 (textbook Fresnel radius and path loss); checked on real terrain with Open-Meteo; USGS answered 0.6–30 s a point that day (some 504s), so a long uncached path takes minutes from it; not yet wired into Sidekick |
| Local-name fix-ups after transcription (`fixups:` in vocab/local.yaml, learned pairs, the net's own name by sound) | working (`station/fixups.py`); 11-03 net: "Aries" x3, "Windlink" x1 |
| Near-miss callsign correction ('W9XVZ' -> W9XYZ heard in this net, 'K7ABD Springfield' -> K7ABC of Springfield); only when one reading clearly wins, what was heard kept (dotted underline + tooltip), live and accurate text, stations heard and Winlink use the corrected call; 15 min after a net ends it runs once more with the whole net known; uncertain ones show as "WV78? KV7RMV KB7XT" to click; expired licences are not ticked in the Winlink To: list | working (`hamkit/callfix.py`, `callfix: false` turns it off); 3.915 net: 8 corrections, none contradicted, most garbles left as heard |
| QRZ.com online lookups (grid, international names), cached for offline use | working (`hamkit.qrz set-login`) |
| Confirmed / can't-be-confirmed mark on every call (FCC Active, current QRZ.com record, roster); an operator's old call moved to the current one by QRZ alias, within VHF/UHF reach | working (`station/consensus.py` `qrz_answers`, `confirm`); 11-03: 4 old calls moved, 17 calls left counted but unconfirmed, each with its reason |
| Q-code / proword / abbreviation explanations | working (`config/glossary.yaml`) |
| Operator hints: relay, check-in, emergency, formal traffic, exercise, CQ/DX, 10-minute ID | working (`config/procedures.yaml`) |
| Net type recognition (emergency, exercise, ARES/RACES, SKYWARN, NTS, check-in, DX) | working, rules-based |
| Live dashboard in the browser (on the station PC or any PC/phone on the network) | working (`station/`, port 8091) |
| The log by what is happening now: On the air (open) or one line for the net on now / Up next; Completed nets and conversations one collapsed line each under a day heading; Stations heard counts the same calls as the log | working (2026-11-04; checked on desktop + phone widths with the first live night's log) |
| Winlink ICS-309 + ICS-213 into the Winlink Express Outbox, To: = everyone who took part | built; Outbox format matched against Winlink Express 1.8 messages (one and two recipients); first real write to be checked |
| Net schedule with radio, frequency and band; editor in the dashboard (Nets) | working (`data/schedule.json`, seeded from `config/station.yaml`) |
| Repeating nets like Outlook: once, daily, every N weeks on days, monthly by day or nth weekday (1st and 3rd Sunday, last Friday), yearly; ends never / on / after N; skip or move one date; said in plain words with the next 5 dates | working (`station/recur.py`); used for listening, the net running now / next, naming nets in the log and the alert; DST, month ends, 5th weekdays and leap years tested |
| Pre-net alert (5 min before, per net): banner + header strip on every dashboard, Radio is set / Remind me / Skip, chime, title, notification on localhost | working (`station/alerts.py`) |
| Net history: each time a net met, with attendance, net control, transmissions, accurate pass, Winlink sent, minutes; backfilled from the logs | working (`station/history.py`, `data/history.db`, SQLite) |
| Search everything (transmissions, stations, nets, minutes) with filters | working (SQLite FTS5, plain LIKE where FTS5 is missing); 100,000 transmissions: ~0.1 s |
| Net analysis: attendance chart, regulars (stated rule), newcomers, regulars missing, net-control rota, every station's times/first/last/streak | working |
| Minutes drafted by rules (attendance, announcements, schedule, action items, decisions, traffic, urgent), editable with a layer that is never overwritten; Still open; .md / text; in the ICS-213 | working (`station/minutes.py`); on the test nets: 100 % of items found, none wrong; on a held-out net written afterwards: 7 of 9 right, 7 of 8 found before tuning. An LLM tidy-up is stubbed (`minutes: {llm: …}`), not wired yet |
| Urgent board for emergency nets (category, reporter, place, people; Handled) | working |
| Listen only during scheduled nets (auto resume 1 min before, pause 5 min after; optional log at the net's frequency) | working, per radio |
| The end of a net: net control's close heard (wording, roll-call-learned net control, questions/plans/quotes excluded), 2 min tail, pause only a radio the station turned on, final accurate pass at once, minutes, Winlink messages ready; End net by hand; Not closed undoes | working (`station/netclose.py`; built from the 2026-11-03 FM net, where the close had no speaker); not yet seen on a live net |
| Accurate second pass on the home server (large-v3), progress + time left on each net and in the Winlink window | working (`accurate:` in station.yaml) |
| Accurate pass WITH CONTEXT: provisional text per transmission (heard with the net before it), final text for the whole net when it goes quiet (`POST /accurate/context`, `transcribe/context.py`) | working (home server; synthetic FM nets: word errors 53 % → 38 %, callsigns found 11 % → 30 %; real 3.915 net: FCC-valid callsigns 39 → 62; see "How the accurate pass works"); older Mac services fall back to one at a time |
| Home server GPU on demand: Whisper loaded on the first request, its memory released after each one, the model unloaded after 10 idle minutes (`transcribe/gpu.py`, `/health` shows it) | working (replayed final passes: 4.5 GB flat instead of growing to 20+ GB; idle: 0.77 GB) |
| Accurate pass ON THIS PC when there is no home server (other operators, other networks, offline): OpenVINO large-v3 on the Intel GPU/CPU or faster-whisper on NVIDIA, chosen by an install-time benchmark; held while the live text needs the PC | working on both station PCs (i5-12600H desktop: final text only; Core Ultra laptop: large-v3 on the GPU at 0.34× real time), see "The accurate pass on this PC" |
| Live engine checked while a dashboard is open: a clear transmission read as one word re-checks the engine on a recent good read and reloads it if broken (`station/core.py`) | working (a fake engine in the tests; the failure seen once after Modern Standby) |
| Recordings and net history kept 6 weeks by default (Settings: 1–104 weeks), swept every 6 hours: recordings, day logs, history database rows (then VACUUM), voiceprints, agent drafts, Grok comparisons; stale raw audio, OpenVINO cache over 3 GB, station.log over 10 MB (`station/retention.py`) | working, 2026-11-12 |
| This PC: measured at full speed (screen held on, the processor's speed recorded, a slowed measurement marked), power settings checked with fixes (installer end + Settings), the keep-awake choice asked once at install, a one-time dashboard notice of what changes, a warning when live transcription runs >4× slower than measured, Measure this PC again (`station/pccheck.py`) | working, 2026-11-12 |
| Full speed while transcribing on laptops: your power plan + mode saved, the installer's HamNet Station plan + Best performance + the screen held on during nets and traffic (+10 min; Settings: or the whole time a radio listens / always / never) or while the accurate pass has work, all put back after (and at the next start after a crash; left on the HamNet plan with nothing saved, it goes back to Windows' Balanced plan and your power mode) (`station/awake.py`, `keep_awake:`) | working on the test laptop: out of Modern Standby by itself, 6 s → 0.5 s for the same work; plan restored after a hard stop |
| Compiled-model cache for the live engine (`data/ov-cache`, shared with the accurate pass) | working; test laptop: turbo on the GPU 36 s → 16 s to load, small.en on the NPU 555 s → 11 s |
| Over-the-air transcription test with word-error score and history | working |
| Local names (Settings: name, meaning, said as a word/letters, sound-alikes, web site, also-heard phrases, nets by name) repaired in the agreement pass (joined forms, web sites said aloud, a guard for plain words), Local terms in the ICS-213, area packs (export / import file or https / installer `area-pack.json` or `-AreaPack`) | working (`station/localnames.py`), 2026-11-10 |
| Tap-to-correct (✎ Fix, log + minutes transcript; Enter/Esc; ▶ while editing; Undo correction): corrected > accurate > live in search, minutes, attendance, ICS-309/213, Winlink; Learned fixes proposed from repeats, applied only on Accept (`data/fixups_learned.yaml`); accuracy against the corrections per radio/mode/net (`/api/accuracy`, `python -m tools.accuracy`) | working (`station/corrections.py`); not yet used on a live net |
| Who is talking: stated callsign / net control by its words / operator's **Who?** (kept in the log, used by the ICS-309) | working (`station/speaker.py`) |
| Who? suggests the closest voice earlier in the same net (only a clear match; you confirm) | working (`station/voicehint.py`) |
| Who is talking by VOICE (speaker embeddings, to fill in transmissions that name nobody) | measured, **not on**: on the 3.915 MHz LSB net same-voice and different-voice clips overlap almost completely (5 WeSpeaker models; first vs second half of one transmission: EER ~31 %, best match right 8–20 %). FM nets, 2026-11-07 (1,880 transmissions over five days, two receivers, 89 clear self-identifications): within one net the closest voice names the speaker right 72–82 % of the time for 31–62 % of transmissions; across days no better than chance (a station's radio and place change). Below the 90 % needed to show it. Code + `python -m tools.voice_eval` kept |
| Look and feel of the companion EMCOMM forms (slate colours, navy top bar, bold clock); Settings → Display: Automatic / Light / Dark / **Night** (red on black, also the read-out card) and a 24-hour (default) or 12-hour clock; ☾ Night in the top bar | built 2026-11-12, checked in Chromium, Firefox and WebKit (Safari), laptop and phone |
| Top bar status in plain words: ● Live text, ● Home server / Accurate on this PC / Home server offline (model names in the tooltip) | built 2026-11-12 |
| Home server comes and goes: checked every minute while idle; lost mid-net → the rest moves to this PC (or waits) at once, a provisional request gives up after 2 min; back → what waited goes at once | built 2026-11-12 (`station/accurate.py`, tests) |
| Winlink messages: the Outbox button is grey with **🔒 Close Winlink Express first** while Express is open, green **✓ Put in Winlink Express Outbox** with a "ready" band once it is closed | built 2026-11-12 |
| Live engine stuck on the same words (2026-11-11 on a laptop station: one sentence or "The.9.9.9" for ~4 h): checked on **every** transmission (no dashboard needed, at most once a minute); repeated / looped / digit-run output is suspect and never the reference; a fresh graphics engine that is stuck too → the live text moves to the CPU and the top bar says **● Live text on CPU** | built 2026-11-12 (`station/core.py`) |
| Sidekick's turn, from the 11-12 review of real nets: who net control called is read around the call ("Any station holding traffic, traffic only. Check in now.", maritime-only calls, "Before I start taking check-ins…", a bare "Please call now." whose start was lost); roll calls by place answered only for **your region** (Settings → Station agent); net control's "W9XYZ, thank you, you're in the log" = checked in, not a question; "break break" / emergency traffic silences it on every net; a net can be set to **never check in**. Replayed on 4 real net days: 11 wrong invitations stopped, none lost | built 2026-11-12 (`tests/test_sidekick_turns.py`) |
| Sidekick's card: **YOUR TURN** chip (Check-ins open to you / CALLED / Checked in ✓ time / Listening: why), **I checked in myself**, **Mute for this net**, the last draft that would have spoken stays in view, silent drafts counted | built 2026-11-12 |
| Sidekick with **no xAI key** or **no internet**: says on its card that it drafts with its offline rules (check in when invited, acknowledge a call; questions wait for the operator), never waits on the network, tries xAI again by itself; **▶ Hear it** uses this computer's own voice | built 2026-11-12, tested both ways |
| Roll calls by place: the US state names are downloaded once from the US Census list (`hamkit/places.py`, 2 KB, into the callbook folder; the installer and the station fetch it), none are in the code; without it the call's shape is used | built 2026-11-12 |
| Urgent board reads **fire** by its grammar: a fire tower, fire seat or fire transducer (fire + a noun) is a thing, "fire it up" is the verb, and a report must be about the word itself ("there's a fire", "fire spreading", "fire on Elm"), not anywhere in the sentence. Replay of 9,475 real transmissions: 8 false alerts gone, no real report lost | fixed 2026-11-12 |
| "This PC is transcribing slowly" is judged on clips of 8 s or more only: a GPU engine costs about 2 s a clip whatever its length, so short clips (an "Over.") looked slow. A day of real traffic: 8 false notices → 0; a PC really 5× slower is still caught | fixed 2026-11-12 |
| **Sidekick card**: a two-sided reference card for setting up Sidekick and reading it on a net (`station/static/sidekick_card.html`, `/help/sidekick`, PDF in `docs/`) | built 2026-11-12 |
| **Sidekick's knowledge library** (`station/knowledge.py`): material Sidekick answers questions and gives prepared talks from, with its sources — this station's own manual, README, technical notes and both cards (re-read when they change), plus the operator's files (.md .txt .html .pdf .docx .csv) in `data\knowledge\`, short notes ("Our EOC's Winlink gateways") and web pages fetched once from a URL; searched by SQLite FTS5 (bm25), the question cleaned of spoken filler with ham abbreviations kept (EOC, ARES, VARA, CW, HF…); 20 MB a file, 200 MB in all; a file it cannot read is listed with why | built + unit-tested 2026-11-12 (`tests/test_knowledge.py`); not yet wired into Sidekick or the dashboard |
| **Net board** per radio (a tab above the log): who is in (numbered), **heard once** marked, **Ask again**, traffic, heard but not checked in, regulars not heard yet, net control's 10-minute ID timer; refreshed after each transmission | built 2026-11-12 (`History.board`, `tests/test_review_board.py`) |
| **Review the check-ins** after a net (✓ / ✎ right call / ✗, ▶ where it was heard): fixes the attendance, the minutes, the ICS-309 and Winlink messages; calls heard once show as **(?)** until reviewed; each mark is logged to `data/review_log.jsonl` as the human truth for call accuracy | built 2026-11-12 |
| Phones: the radio column scrolls as a whole when the notices and Sidekick's card are taller than the screen (the tabs under them were out of reach) | fixed 2026-11-12 |
| Read-out end to end with the forms project's real EOC server (`tests/test_forms_e2e.py`, `EMCOMM_FORMS_REPO=…`); the two copies of the contract checked identical | tested 2026-11-12 against forms 2026-11-12.3 |

## License

MIT — see [LICENSE](LICENSE). Use it, change it and share it, for your club, ARES group or EOC.

## About

HamNet Station was created as a volunteer contribution to the amateur radio and EMCOMM (emergency communications) community, in the same family as the [Church of Jesus Christ EMCOMM forms](https://github.com/travislhilton/church-emcomm-wnlk-forms). It is offered freely to any club, ARES group or EOC that can use it. It is an independent project, not an official product of any church, club or agency.
