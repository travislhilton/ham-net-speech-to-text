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
3. Type your callsign when asked. The first run downloads about 4 GB (Python packages, Whisper
   models, the FCC license database) and takes 10–20 minutes. Re-run `install.cmd` after every update.

What the installer does (`install/windows/install.ps1`, safe to re-run). Nothing needs to be installed beforehand:
- checks the PC first (64-bit Windows 10/11, about 8 GB free, internet for the first install) and says what is missing;
- installs the Microsoft Visual C++ runtime if it is missing (OpenVINO, PyTorch and faster-whisper need it);
- installs Python 3.12 for all users (winget, or straight from python.org when winget is missing) and a private
  environment in `C:\ham-net-speech-to-text\.venv` with every Python package the station uses;
- downloads four Whisper models in Intel OpenVINO format (three for the live text, Whisper large-v3 for the
  accurate pass; plus faster-whisper large-v3 on a PC with an NVIDIA card) and **benchmarks this PC** to choose the
  live engine (`tools/pick_engine.py` → `config/engine.yaml`): Intel graphics, NVIDIA (CUDA) or CPU;
- **measures what this PC can do for the accurate pass on its own** (`tools/pick_accurate.py` → `accurate:` in
  `config/engine.yaml`): each accurate model on each device (Intel GPU, NPU, CPU; NVIDIA), then decides *both
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
**Stations heard**, all the same height, so the log always fills the space below
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
  **A scheduled net is always recorded:** a paused radio is resumed 1 minute before each of its nets and paused
  again 5 minutes after it (if you press Pause or Resume during the net, your choice stands). "Skip this net" on
  the pre-net alert is the way to not record one occurrence.
  Remembered across restarts.
- **Clear log…** (on each radio card): clears that radio's log and stations heard from the screen. The day's
  log file and the recordings stay on the station PC (`data\logs`, `data\recordings`); nothing is deleted.
- **More room for the log on a phone:** tap the open tab (or **▴ Hide**) to fold the radio panel down to its tab
  bar; **▾ Show** or any tab brings it back (remembered per radio). Each net's header folds too (tap its title; open
  by default, remembered).
- **The log** (under each radio's card, one per radio), arranged by what is happening now:
  - **On the air** at the top: the net or conversation going on now (heard within the last 15 minutes and not
    closed by net control), open, its header pinned while you scroll its transmissions, newest first. The tag says
    **on the air**, **running over** (past its scheduled end) or **closing** (net control closed it; recording the
    73s).
  - When nothing is on the air, one line instead: the scheduled net in its time now, still waiting for its first
    transmission (**on the air**, or **radio paused**: press Resume to record it), else **up next**: the next net
    on this radio with its day, time, frequency and a countdown. Tap it to open the net in Nets.
  - **Completed**: every earlier net and conversation, one line each under a day heading (Today, Yesterday, …):
    time, name, Closed/Ended, ✓ accurate (or the accurate pass's progress), frequency, transmissions and stations.
    They start collapsed; open one for its Minutes, Winlink messages, close and accurate-pass status and every
    transmission (the ones you open stay open in this browser). A net moves here when it is closed or 15 minutes
    after it goes quiet. After a restart the log still holds yesterday's nets as well as today's; older ones are in
    Nets → History and Search.
  - A paused radio folds its meters and waterfall away, leaving more room for the log.

  Each transmission: grey words appear while the station is still
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
- **Correcting a line** (**✎ Fix** on any finished transmission, in the log and in a net's Minutes → Transcript):
  the words open in place, ready to edit, with ▶ to listen while you type. **Enter** (or Save) keeps it, **Esc**
  (or Cancel) leaves it. Your text then wins everywhere: the log, search, the minutes and attendance, the ICS-309 /
  ICS-213 and the Winlink messages (your text, then the accurate pass, then the live text), and the callsigns and
  speaker are read again from it. A corrected line shows **✎ corrected**; tap it to change it again or
  **Undo correction**. It is kept as an update line in the day's log (survives restarts, the accurate pass and a
  rebuild of the history), other open dashboards update at once, and it works for a net from any day.
  - **Learned fixes** (Settings): when the same words are corrected the same way in two transmissions ("shelby
    ville" → Shelbyville), or once into a callsign, the station proposes it as a fix-up. Nothing changes until you
    press **Accept**: accepted ones go to `data\fixups_learned.yaml` (the `fixups:` format of `config/vocab.yaml`)
    and correct what the station hears from then on; **Ignore** hides a proposal, **Remove** takes one back out.
    The number on the Settings button is how many are waiting.
  - **Accuracy (from your corrections)** (in **Test transcription…**, under the test history): the word error rate
    of the live text, the accurate pass and the consensus text against everything you corrected, per radio, mode
    and net, with how many lines it is based on (under 20 is a rough guide; your eye starts from the text shown).
    The same from a terminal: `python -m tools.accuracy` (or `--data data` with the station stopped).
- **What you can do now** (per radio): step-by-step help and example phrases when someone asks for a relay,
  calls emergency or priority traffic, opens a net, takes check-ins or passes formal traffic, plus a
  reminder when your own 10-minute identification is due.
- **Stations heard** (per radio, a tab above the log; the same calls as the log: your corrections, then the
  net-wide agreement, then the accurate pass, then the live text): one line per callsign (city, state, country · name, how
  often and when last heard); tap a line for the license, grid, zones and address (FCC, QRZ.com and the country file). Click any callsign for its card (with
  the nets that station takes part in: how many times and when last).
- **Net type** (a badge on each radio card): the kind of net being heard (ARES/RACES, SKYWARN, exercise, check-in, traffic, DX…),
  with a red banner if it sounds like a real emergency.
- **Search everything** (the box at the top): every transmission ever heard (live and accurate text), callsigns and
  callbook names, nets, and the minutes, in one list grouped as **Stations**, **Nets**, **Minutes** and
  **Transmissions**, with the words highlighted, ▶ to play and **Show in context ›** (today's log, or the net's
  minutes with the transmission lit up). Several words must all match; "quotes" for an exact phrase. Narrow it by net,
  radio, dates or callsign. Searching 100,000 transmissions takes about a tenth of a second.
- **Nets** (on each radio card): the nets scheduled on that radio, the one running now in green, the next date of the
  others.
- **Nets** (top of the page; **Nets…** on a radio card opens it for that radio). One window, full screen on a phone, with
  five tabs and a **Net** picker:
  - **Schedule**: each net as a card in plain words ("Every 1st and 3rd Sunday at 19:00, 146.940 MHz FM, 60 min"),
    its next date, net control and alert. **Add a net** / **Edit**: name, radio, start time, minutes, frequency and
    mode, net control, **Alert, minutes before** (5 by default, 0 = none), **Emergency or priority net** (keeps an
    Urgent board), and **Repeats** like Outlook: **Once**, **Daily**, **Weekly** (every N weeks on chosen days),
    **Monthly** (on day 1–31 or the last day, or on the 1st/2nd/3rd/4th/5th/last chosen weekday, any mix such as
    1st and 3rd Sunday), **Yearly** (a month plus a day or an nth weekday), every N, starting on a date, ending never,
    on a date or after N times. A day a month lacks (the 31st, the 5th Sunday) means its last day / is skipped. While
    you type, the rule is said in words with its **next 5 dates**; on a saved net each date has **Skip** (cancelled,
    **Restore** brings it back) and **Move…** (another day, time or frequency for that one date only). Changes apply
    at once on every open browser. The schedule starts as the `schedule:` list in `config\station.yaml`; the first
    change made here is saved to `data\schedule.json`, which from then on is the schedule (station.yaml is never
    rewritten; delete schedule.json to start again from station.yaml). Old entries with `days: [Tue, Thu]` keep
    working: they are read as a weekly rule and saved in the new form the next time they are edited.
  - **History**: every time each net met, newest first by month: date, time, stations, transmissions, accurate pass
    done, minute items, urgent issues and whether its Winlink messages went out. Tick **Include conversations** for
    what was heard outside any net. Tap one for its minutes. Kept in `data\history.db`, built from `data\logs` the
    first time the station starts (older logs included) and kept up to date as transmissions are logged.
  - **Analysis** (per net): times met, stations on average, the **stations each time** chart (a table too), last
    time's **newcomers** and **regulars not heard**, the **regulars** with the rule written under them (took part in
    at least half of the last 8 times, and at least twice), the **net control** rota, and every station that ever took
    part: times, of the last 8, first and last time, and streak. Taking part = spoke, checked in, or was
    acknowledged by net control; a callsign only mentioned does not count.
  - **Minutes** (per time the net met; also **Minutes** on each net's header in the log): drafted from the
    transcript by rules as the net runs, offline: **attendance** in check-in order with net control, **urgent**
    issues, **announcements** (with the date or place they are about), **schedule** (events), **action items**
    (who, due), **decisions** and **traffic**, each linked to the transmission it came from. Items the station drafted
    say *suggested*: tick **done**, **Edit**, or **Not right** (hidden, and not drafted again). On a net that is not
    an emergency net, a lone alarm word ("fire", "ambulance", "crash") is urgent only with something reported
    ("there's a fire on Main", "we need an ambulance at…") or a place; a fire station called by its name
    ("Springfield Fire, go ahead") and check-in talk never raise the Urgent banner. Add items, add or remove
    a station from the attendance, set net control, write **Notes**. Your changes are a layer of their own and never
    overwritten when the net is drafted again. Open action items and traffic carry forward to the next time. **‹ ›**
    step to the time before/after. **Download .md** or **Copy as text**; **Winlink messages…** puts the minutes in
    the ICS-213.
  - **Still open** (per net or for all): what net control may read out: urgent issues not handled, announcements still
    valid (until their date passes, or 4 weeks when undated), coming events, open action items and traffic not yet
    handled. Shown again with the pre-net alert and when the net starts.
- **A net that runs over**: past its scheduled end, a net still being heard keeps recording and keeps its
  transmissions (header and Nets today: **running over**) until net control closes it, it is quiet for 5 minutes
  (`overrun_quiet_min`), or 1 minute before the next net on that radio; at most 3 hours over (`overrun_max_min`).
  Then the usual end of a net: the final accurate pass, the minutes, and the radio paused if the station resumed it.
- **Nets today**: a strip under the header lists, one line each, the nets still to come today and the one on the
  air (time, name, frequency, radio). The next one is highlighted; one on the air shows **on the air**, or
  **radio paused** when its radio is not recording. Tap a line to open it in Nets.
- **Pre-net alert**: 5 minutes before each net (per net, **Alert, minutes before**), a banner on that radio's column
  and a strip under the header on every open dashboard: "Tuesday Net starts in 5 min (19:00): set FTM-150R to
  146.940 MHz FM", with what is still open for that net. **Radio is set** (also logs the radio at that frequency and
  mode, like Change), **Remind me in 1 min**, **Skip this net** (a radio that listens only during nets then stays
  paused for it). If the radio is already logged on that frequency it says **Radio already on 146.940 — ready**. It
  stays up until 10 minutes after the start. A chime plays (🔔 on the banner or Settings mutes it, remembered per
  browser) and the tab title blinks. On the station PC itself (`http://localhost:8091`) **Settings → Allow
  notifications** also shows it as a Windows notification; browsers allow that only on localhost or https, so on a
  phone the banner shows while the page is open (and the chime needs one tap on the page first).
- **Urgent board** (emergency nets, and anything clearly urgent on any net: injury, fire, flood, missing person,
  medical, shelter, power, roads, resources): a red box pinned above the log of the radio it was heard on, one line
  each with its category, who reported it, when and where, and the people named. **Handled** clears it; **Show ›**
  jumps to the transmission. It never covers the log (**Hide** folds it).
- **Listening** (Nets → Schedule): per radio, **Listen only during scheduled nets**: the station resumes that radio 1 minute before each of its
  nets and pauses it 5 minutes after the net ends, so nothing between nets is recorded. The card says why it is
  paused ("Paused until Example County ARES Net at 20:00" or "Paused by you"). Pause and Resume still work; the next
  net start or end takes over again. Optionally, **when a net starts, log the radio at the net's frequency and
  mode** (you still tune the radio yourself; the retune warning stays). Kept across restarts in `data\operator.json`.
- **Nets and conversations**: each radio's log is split into nets/conversations (a new one starts after 15
  quiet minutes, when the radio's frequency changes, when a scheduled net starts, or after a net is closed), each with a header: the net's
  name from the schedule (or the kind of net heard), time span, frequency, how many transmissions and stations, and
  its own **Minutes** and **Winlink messages…** buttons. That builds an **ICS-309**
  Communications Log (every transmission: time, from = who is talking as above — yours, then stated, then net
  control, "?" when unknown —, to, what was said; 30 rows a page) and an **ICS-213**
  summary (stations heard, traffic and relays, and the net's minutes) for that net only, addressed to every callsign that took part
  (callsigns no callbook confirms start unticked; **Uncheck all / Check all / Only callbook-confirmed**, and add
  any callsign). Edit, then **Put in Winlink Express Outbox**; Winlink Express sends them the next time you
  connect. Close Winlink Express first; the station refuses while it is open and backs up Winlink's message
  list before each write.
- **The end of a net** (`station/netclose.py`). **Net control closes it:** when net control says so on the air
  ("hearing none, we'll go ahead and close the net", "this net is now closed", "closing the net", "secure the net",
  "this concludes tonight's net", "returning the repeater to normal amateur use"; "QRT" and "N7XYZ clear" only from
  net control), the header shows **Closed 20:15** and "Net closed by N7XYZ at 20:15". Questions and plans are not a
  close ("before we close the net, any…?", "we'll close the net at 9"; "the net will close in 5 minutes" shows
  *closing soon*). **A goodbye to everyone** near the end also closes it ("thank you all for your participation",
  "I want you all to have a safe and wonderful evening", "good night everyone"): only within 10 minutes of the
  scheduled end or while it runs over, after 20 minutes of net, from net control (or a speaker not known to be
  someone else), and never one said to a single station ("thanks for checking in, Bob, have a good night"); the
  recording then goes on 5 minutes, not 2. Who counts: the schedule's net control; else the station that gave its call while opening the net
  or taking roll ("This is N7XYZ, roll call will begin now"; the header offers **Save N7XYZ as net control for
  <net>**); when nobody can tell who is talking (usual on FM: net control rarely says its call when closing), net
  control's closing words count after 5 minutes of net, inside the scheduled time (start to end + 5 min). The
  station keeps recording **2 more minutes** for the 73s and IDs (`close_tail_min`), then: the radio is **paused**
  if the station turned it on for this net (it was paused before the net, or it listens only during nets) and you
  have not pressed Pause or Resume since; a radio you had listening before the net keeps listening, and a
  conversation outside the schedule never pauses anything. The **final accurate pass starts at once** (no waiting
  for 5 quiet minutes), the minutes draft is refreshed, and what is heard afterwards starts a new conversation. When
  the pass is done the header offers **Winlink messages ready**: the ICS-309 and ICS-213 built from the accurate
  text, for you to review (nothing goes into the Outbox until you press Put in Outbox). **Keep listening** (during
  the 2 minutes) or **Not closed** (after) undoes it: the radio listens again if this paused it, the net runs to its
  scheduled end and the final pass runs later as usual. Without a close nothing changes: a net ends at its
  scheduled end + 5 minutes. **End net** (on the header of the net or conversation going on, and on its radio's
  card) ends it yourself: an inline "End the net now? It stops recording on FTM-150R…" **End net** / **Cancel**,
  then no tail: recording stops (the radio is paused, as if you pressed Pause; its later nets still resume it),
  the final pass starts, and **Winlink messages ready** appears when it is done. `close_detect: false` turns the
  listening for closes off (End net stays). Kept in `data\netclose.json`.
- **One station, one call, across the net** (net-wide agreement). Whisper hears the same station several ways
  in one net ("KD7ABP" and "KD7ABD"), clips the prefix ("87QF"), or writes a call the transcript did not list.
  Every minute, for each net heard in the last 12 hours whose text changed (and once more when the final accurate
  text is in), the station weighs, for every call heard, the calls it could be: the other calls heard in the net
  one or two sound-alike letters away, and FCC licences that close. The evidence: an **Active FCC licence**; the
  licensee's **first name said with the call** (right after it, or in net control's reply that follows it
  directly: "Thank you, Bob"; never past the next other call; nicknames count: Bob = Robert, Wally = Wallace);
  **the town said** being the licence's city, or the licence being in your state; **earlier check-ins to this same
  net** (the history); how often it was heard. A call only changes when one reading clearly wins, and never: onto
  a call heard in the same transmission; between two calls each heard in its own check-in turn (unless they differ
  by one letter and one is no live licence); by more than one letter unless the name was said with the call, or a
  name and earlier check-ins agree; a live licence on "in your state" alone. One name moves one call only. A
  clipped call ("87QF", "he's 7QF": up to two leading characters lost) is completed only when exactly one Active
  licence in your state ends that way and its holder's first name is said with the call or in net control's direct
  reply, or it checked in to this net before; when two or more fit, the one of them heard in full in the same net
  is used ("7QH" with N7QH checked in earlier), otherwise it is left as heard, with the candidates.
  **On VHF/UHF, distance instead of state:** a 2 m or 70 cm net hears the stations in range, so with the ZIP
  centres installed (`python -m hamkit.zipgeo update`; the installer does it) a licence's mailing ZIP is measured
  from the station (`station_lat`/`station_lon`, else your 6-character grid in Settings, else your own licence's
  ZIP). A licence within 150 km scores as in range ("14 km away"), one beyond as "beyond VHF/UHF range". A
  clipped call goes to the clearly nearest licence (within 150 km, every other one at least 2.5× and 50 km
  farther), across a state line too; the nearest within 50 km needs no name, a farther one does. Two close
  licences stay as heard, each with its distance. On HF the state rule above applies. A mailing address is not
  always where the station is (a PO box, a visitor), so distance only ranks licences that fit what was heard.
  A licence beyond range said in one transmission only, with no name, town, earlier check-in or roster entry for
  it, is almost always a misreading (11-03: 32 of 33 licences beyond 250 km were heard once): it stays in the
  text, shown faded and struck through ("heard once, 412 km away"), and is left out of the station counts,
  Stations heard, attendance and the Winlink messages. ✎ Fix puts it back if it was really there. A licence never heard needs a name, a town or a history
  behind it, and a call nothing supports stays as heard, marked *unverified*. The transmission keeps its heard text; its
  calls, Stations heard, attendance and minutes, and the Winlink ICS-309/213 use the agreed calls (tooltip: "heard
  KD7ABP, corrected: FCC: active; name Bob matches; licensed in IL"). Re-run a past day (stop the station first):
  `.\.venv\Scripts\python.exe -m tools.consensus 2026-11-03` (`--dry-run` only reports; running it again changes
  nothing). `consensus: false` in `station.yaml` turns it off.
- **Local names fixed after transcription** (never put in Whisper's prompt, which makes Whisper echo the prompt):
  `fixups:` in `config\vocab.yaml` (ARES heard as "Aries" — only when the sentence is about a net, members, the
  county…; "Windlink" -> Winlink; "nest control" -> net control) plus your own in `config\local.yaml` (same
  format: `- {heard: Springfeld, say: Springfield}`, optional `context:` regex) and the pairs the dashboard learns
  from your corrections (`data\fixups_learned.yaml`). The scheduled net's own name is repaired by sound when it
  clearly is that name ("Springfeld Country Aries net" -> "Springfield County ARES net"): put the name as net
  control says it in the schedule entry (`spoken: [Springfield County ARES net]`) or in `local.yaml`
  (`net_names: [...]`). The text shown, the minutes and the forms use the fixed text; the heard text is kept.
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
  **Without the home server** (any other operator's station, another network, no internet at all) the same pass
  runs **on the station PC itself**, on its spare compute, when the installer's benchmark says the PC can do it
  (see "The accurate pass on this PC" below). The header and the transcript's tooltip say where each text was
  made, e.g. *whisper-large-v3-int8-ov@GPU (this PC)*.
- **Test transcription…** (on each radio card): someone reads a fixed text over the air; **Score** shows how
  many words the live text and the accurate pass got wrong (0% = all right) and keeps a history, so a change
  to the radio, the cable or the settings can be judged by a number (`data\tests.jsonl`).
- **Help** (top right): opens the two-sided reference card (`/help`, from `station/static/card.html`) in a new tab.
  Print it double-sided on letter paper (flip on the long edge) and keep it by the radio.
- **Settings** (top right): your callsign, name and grid; which Winlink Express folder (found
  automatically: `C:\\RMS Express` and any other install) and which callsign account sends; **Net alerts**: the chime, and
  notifications on the station PC.

Which radios exist (and their sound inputs) and the first nets are set in `C:\ham-net-speech-to-text\config\station.yaml` (every option is explained in
`config\station.example.yaml`). After editing it, restart the station: Task Scheduler → **HamNet Station** →
End, then Run (or sign out and back in). Everything heard is kept in `data\logs\<date>.jsonl` and
`data\recordings\<date>\`; the nets' history, minutes and search index in `data\history.db` (rebuilt from the logs if
deleted, except your edits to the minutes); problems are written to `data\station.log`.

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
| Repeaters / carrier that stays up between speakers: split at speech pauses (>= 0.8 s) and carrier dips, cut in the middle of the pause, a fragment ("Over.", the end of a call sign) kept with its neighbour, a voiceless repeater tail never its own transmission | working (`split_pause_ms`, `dip_split_ms`, carrier-gate radios; see "Segmenting"); 2026-11-03 FM net replayed: 140 → 105 transmissions, blank ones 31 → 3, words lost in dropped pieces 79 → 0 |
| HF / voice gate: 600 ms pre-roll; a short piece waits 0.6 s after the hangover for its continuation (a call sign spelled with gaps, a kerchunk before the words) | built + unit-tested (`merge_gap_ms`, `merge_max_s`, `preroll_ms`); not yet measured on a live HF net |
| Sub-audible tone + mains hum removed before Whisper (high-pass 300 Hz on FM data-jack radios) | working (`highpass_hz`) |
| Whisper's noise phrases ("So.", "We'll be right back.") shown greyed as probable noise, left out of the ICS-309 | working |
| Icom IC-705 via Icom Remote Utility (voice gate) | working on the test PC; frequency read from the radio planned |
| Live transcription on the PC (engine picked per PC; W9XYZ-5: turbo on Intel graphics, final text 2.4 s after unkey for an 8.5 s transmission) | working |
| Words while someone is still talking (base.en, updated each second, replaced by the final text) | working |
| FCC callbook (1.7 M licenses, daily updates) + country lookup for any callsign | working |
| Net-wide callsign agreement: variants of one station merged by FCC licence, first name, town, earlier check-ins; clipped and unlisted calls found only with a name/town/history; nothing supported = kept, *unverified* | working (`station/consensus.py`, `tools/consensus.py`, `consensus: false`); 11-03 FM net (140 transmissions, FCC on the dev Mac, no history yet): 11 readings moved, 5 calls found in the text, none contradicted by the transcript, the ambiguous ones left as heard. First live night (3 nets): 41 moves, several over-reached (two letters on a name heard in the reply, one name used twice, merging a call heard as its own station); the rules above now keep 25 of them and drop 16 (dry run, same day); clipped calls back under the one-licence rule: 1 of 4 completed (the other 3 have two in-state licences each, left as heard) |
| VHF/UHF callsign distance (Census ZIP centres): clipped calls to the clearly nearest licence, in-range scoring instead of the state, out-of-range calls said once not counted | working (`hamkit/zipgeo.py`); 11-03 nets: every station heard 3+ times was within 50 km, 32 of 33 licences beyond 250 km were heard once (misreadings) |
| Local-name fix-ups after transcription (`fixups:` in vocab/local.yaml, learned pairs, the net's own name by sound) | working (`station/fixups.py`); 11-03 net: "Aries" x3, "Windlink" x1 |
| Near-miss callsign correction ('W9XVZ' -> W9XYZ heard in this net, 'K7ABD Springfield' -> K7ABC of Springfield); only when one reading clearly wins, what was heard kept (dotted underline + tooltip), live and accurate text, stations heard and Winlink use the corrected call; 15 min after a net ends it runs once more with the whole net known; uncertain ones show as "WV78? KV7RMV KB7XT" to click; expired licences are not ticked in the Winlink To: list | working (`hamkit/callfix.py`, `callfix: false` turns it off); 3.915 net: 8 corrections, none contradicted, most garbles left as heard |
| QRZ.com online lookups (grid, international names), cached for offline use | working (`hamkit.qrz set-login`) |
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
| Accurate pass WITH CONTEXT: provisional text per transmission (heard with the net before it), final text for the whole net when it goes quiet (`POST /accurate/context`, `transcribe/context.py`) | built + measured, not deployed yet (synthetic FM nets: word errors 53 % → 38 %, callsigns found 11 % → 30 %; real 3.915 net: FCC-valid callsigns 39 → 62; see "How the accurate pass works"); older Mac services fall back to one at a time |
| Accurate pass ON THIS PC when there is no home server (other operators, other networks, offline): OpenVINO large-v3 on the Intel GPU/NPU/CPU or faster-whisper on NVIDIA, chosen by an install-time benchmark; held while the live text needs the PC | built + measured on an i5-12600H station PC, not deployed yet (see "The accurate pass on this PC") |
| Over-the-air transcription test with word-error score and history | working |
| Tap-to-correct (✎ Fix, log + minutes transcript; Enter/Esc; ▶ while editing; Undo correction): corrected > accurate > live in search, minutes, attendance, ICS-309/213, Winlink; Learned fixes proposed from repeats, applied only on Accept (`data/fixups_learned.yaml`); accuracy against the corrections per radio/mode/net (`/api/accuracy`, `python -m tools.accuracy`) | working (`station/corrections.py`); not yet used on a live net |
| Who is talking: stated callsign / net control by its words / operator's **Who?** (kept in the log, used by the ICS-309) | working (`station/speaker.py`) |
| Who is talking by VOICE (speaker embeddings, to fill in transmissions that name nobody) | measured, **not on**: on the 3.915 MHz LSB net same-voice and different-voice clips overlap almost completely (5 WeSpeaker models; first vs second half of one transmission: EER ~31 %, best match right 8–20 %). Code + `python -m tools.voice_eval` kept to re-measure on FM nets |

## Segmenting

How the station turns a radio's audio into transmissions (`station/segmenter.py`; per-radio settings in
`config/station.yaml`, defaults per gate in `Station.SEG_DEFAULTS`):

| Setting | FM carrier gate | HF voice gate | What it does |
|---|---|---|---|
| `preroll_ms` | 500 | 600 | audio kept from before the gate opened; on HF the voice gate hears a first syllable late |
| `hangover_ms` | 900 | 900 | the gate must stay closed this long before a transmission ends |
| `min_speech_ms` | 400 | 400 | less speech than this is a kerchunk and dropped (unless merged, below) |
| `split_pause_ms` | 800 | 0 (off) | a carrier that stays up (repeater hang time) is split where the voice pauses this long |
| `dip_split_ms` | 96 | 0 | a carrier dip this long (the last speaker's squelch tail) is a turn boundary even with a shorter pause |
| `merge_gap_ms` | 0 (off) | 600 | a short piece waits this long after the hangover; if the gate opens again it is the same transmission |
| `merge_max_s` | 3.0 | 3.0 | only pieces with less speech than this wait for a continuation |
| `min_split_s` | 2.0 | 2.0 | no piece is cut off shorter than this |
| `pause_ratio` | 0.35 | 0.35 | how far between this radio's quiet and loud (gate open, last 30 s) a frame must be to count as voice |

Rules for a carrier that stays up: the cut is made when the voice comes back, in the middle of the pause, so
neither piece starts or ends on a word; the earlier piece is only sent for transcription once the next one has
`min_speech_ms` of voice, so a fragment stays with its neighbour instead of becoming its own transmission (or being
dropped), and a voiceless repeater tail after the last word is trimmed. A dead carrier before anyone talks is dropped.
Merging happens before a transmission ends, so the dashboard and the accurate pass only ever see the joined audio
(one transmission id); there is nothing to replace afterwards.

Measured on the 2026-11-03 FM repeater net (24 minutes of the station's raw recording replayed through the old and
the new segmenter, every piece transcribed with Whisper large-v3-turbo):

| | before | after |
|---|---|---|
| transmissions | 140 (+36 dropped) | 105 (+0 dropped) |
| blank pieces ("." / "So." with no words) | 31 | 3 |
| words (of 989 heard in the whole carrier) in no transmission | 80 | 1 |
| cuts inside a carrier / words a cut ran through | 103 / 29 | 32 / 9 (word-timing jitter: the text on both sides is whole) |

What it found: the voice-pause detector learned its "loud" level from the open-squelch hiss between overs, which is
louder in the voice band than a weak station, so a weak station's whole check-in read as a pause: cut mid-call, then
dropped as a kerchunk (a check-in "K7ABC ... power" lost that way). Speech never started before the carrier on
the FM net (76 carrier openings), so on FM the pre-roll is lead-in, not lost syllables; a call sign read with its
prefix wrong ("87ABC") is Whisper's reading of a whole word, not a clipped start.

## How the accurate pass works

The station cuts a repeater net at its 0.8 s pauses (see "Segmenting"), so one "transmission" is often half a
sentence, or one speaker's tail and the next speaker's start, a few seconds long. Whisper given such a piece on its own reads courtesy beeps
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

## The accurate pass on this PC

Another operator's station (another network, or no internet at all) never reaches the home server, so the
same method (`transcribe/context.py`) runs on the station PC's own spare compute. Order per job: the home server when
a key is set and it answers, else this PC, else the job waits and is retried.

- **What it runs** (`transcribe/decoders.py`): OpenVINO GenAI `WhisperPipeline` with word timestamps on the Intel
  GPU, NPU or CPU (model `whisper-large-v3-int8-ov`, 1.5 GB, downloaded by the installer), or faster-whisper
  (CTranslate2) on an NVIDIA card. Whisper's 30 s long-form walk is done one window at a time by the station's own
  code, so the pass can stop between windows. OpenVINO gives word *times* but no word *probabilities*: the merge of
  the two decodes then prefers the words farther from a window cut, a lone "Thank you."-style text is always set
  aside, and there is no low-confidence test (measured on the home server with the probabilities removed: 2–4 points more
  word errors, see below).
- **What the PC can sustain** (`tools/pick_accurate.py`, run by the installer; re-run after a hardware change):
  each model on each device decodes a ~55 s test block; rtf = seconds of work per second of audio. Large-v3 if it
  runs at rtf ≤ 1.0 (the final text of a net then arrives within ~2.5× its speaking time), else turbo, else none; a
  an NPU wins when it is at most 2× slower than the fastest device (it has no live text on it), otherwise the
  fastest device — not the CPU just because the live text is on the GPU (measured below: the CPU slowed the live
  text more than sharing the GPU did); **both stages** only at rtf ≤ 0.12 (a provisional text for every transmission), otherwise **final text
  only** (after each net). Written to `config\engine.yaml` (`accurate:`).
- **The live text always wins**: the pass runs in its own process (`python -m station.accurate_local`) at
  below-normal priority, with its own model (never the live engine's), on the GPU at low queue priority. It is held
  while any radio is receiving or a live final is queued or being transcribed, and for 2 s after (`quiet_s`); it
  stops between 30 s windows, so at most one window (seconds) overlaps a new transmission. The model is loaded for
  the first job and the process ended after 10 idle minutes (`unload_after_min`), giving the memory back. On a
  laptop on battery it does not run unless `local_on_battery: true`.
- Settings (`config\station.yaml`, `accurate:`): `local: auto | final_only | off`, `local_on_battery`, `quiet_s`,
  `unload_after_min`.

Measured 2026-11-03 on an i5-12600H PC with Iris Xe graphics, 16 GB (live text: turbo on the GPU, base.en on the
CPU), OpenVINO 2026.4:

| accurate model @ device | rtf (one decode) | memory | |
|---|---|---|---|
| large-v3 int8 @ GPU | 0.62 | 3.8 GB | final pass of a net: 1.4–1.7 s per second of audio |
| large-v3 int8 @ CPU | 0.68 | 4.1 GB | |
| turbo int8 @ GPU | 0.22 | 2.3 GB | much less accurate (below) |
| turbo int8 @ CPU | 0.37 | 2.0 GB | |
| faster-whisper large-v3 int8 @ CPU | > 2 | 9.5 GB | stopped: too slow and too big for this PC |

So this PC does **the final text only**, about 1.5× the net's speaking time after the net (none of these is fast
enough to follow every transmission). Live latency (an 8.5 s transmission on turbo, GPU) while the pass ran on the
same GPU: median 3.06 s alone, 3.24 s with the pass held as above, 3.29 s with it never held (max 3.6 s). With
the pass on the CPU instead: 3.03 s alone, 3.46 s held, 3.52 s never held — and only ~0.6 GB of memory left to
promise, against ~1.6 GB on the GPU. Before loading, the pass checks that its model's memory plus 1 GB is free
(RAM + page file) and otherwise waits; a 16 GB PC with a small page file has room for one accurate model.
Word errors on the synthetic test nets (`tools/accbench`, final pass, same nets and scoring as the home server's
figures above):

| | FM nets (4) | SSB moderate (2) | all 6 | callsign precision |
|---|---|---|---|---|
| live text | 55 % | 51 % | 54 % | 45 % |
| home server (mlx large-v3, with word probabilities) | 39 % | 29 % | 36 % | 60 % |
| home server without word probabilities | 42 % | 33 % | 39 % | 61 % |
| **this PC** (OpenVINO large-v3 int8, Intel GPU) | 40 % | 38 % | 39 % | 65 % |

On FM this PC is within a point of the home server; on SSB it is ~10 points behind (no word probabilities, and the
int8 model hears weak SSB a little worse), still far better than the live text. The final pass took 1.4–1.7 s per second of audio (each 4-minute
net 5–7 minutes).

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
