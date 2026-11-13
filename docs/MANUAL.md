# HamNet Station — the manual

Every feature of the station in detail. New to it? Start with the [README](../README.md): install, first steps,
[running a net](../README.md#running-a-net) and the quick reference card. How it works inside:
[TECHNICAL.md](TECHNICAL.md).

## On a laptop

Measured on a 2026 Intel Core Ultra laptop with Modern Standby and an OLED screen): such laptops
slow themselves to about a fifth of their speed whenever the screen is dark, even on mains power with sleep set to
never. So while the station is transcribing (by default: during a scheduled net, while a transmission is received or
transcribed or the accurate pass has work, and for 10 minutes after the last transmission) it saves your own
Windows power plan and power mode, switches to the installer's **HamNet Station** plan (screen on, no sleep, lid does
nothing, USB / Wi-Fi / PCIe power saving off, Intel graphics at maximum) with Best performance, and holds the screen
on; when it is done it puts your plan and mode back (also at its next start, if it stopped mid-net). Your own plan
can favour the battery. Measured: the station's hold brought the laptop out of Modern Standby by itself, and the
same work took 0.5 s instead of 6 s. The hold starts at a transmission's first moment, so a dark screen between
nets costs nothing. **Settings → Full speed and screen on**: *during nets and traffic* (the default, `traffic`), *the
whole time a radio listens* (`listening`: with a radio always on and no schedule, that is around the clock — the
owner's laptop stayed lit all evening that way), *always*, *never* (`off`); `keep_awake:` in `station.yaml` is the
starting value, `keep_awake_quiet_min:` (10) the minutes after traffic.
**Settings → This PC** (`station/pccheck.py`) says what the station changes and whether it is holding right now (and
why), shows the speeds this PC measured and the processor's speed while it measured, lists the power settings that
slow it (on battery, Best power efficiency, hibernate on mains power, Modern Standby, a graphics driver over 18 months
old, a processor held back even at full power = the maker's quiet / fan profile, with the maker's app named) and has
**Measure this PC again** (between nets; the station restarts to use the result). The installer measures with the
screen and system held awake on the HamNet plan (`python -m station.pccheck measure`), asks the *Full speed and
screen on* question once (`-KeepAwake traffic|listening|always|off` answers it unattended) and ends with the same
**This PC** list. The dashboard shows a one-time notice of what the station changes, and a warning when live
transcription runs over 4× slower than measured (screen off, battery, a quiet profile).
**While the dashboard is open in a browser** (any tab, and for 2 minutes after the last one closes) the default
also holds the whole time a radio listens, and the station checks its live engine: when a transmission of 3 s or more
comes back as one word or nothing, it re-reads a recent transmission that read well; if the engine cannot read that
any more, it loads a fresh one and reads the transmission again (logged as "live engine: … reloading it"; at most
once per 5 minutes). The same check runs when a dashboard is opened. Measured on the test laptop: after Modern
Standby between transmissions, its live engine wrote "The." for every transmission until it was reloaded.
With "Listen only during nets" on a radio, the HamNet plan is on only during nets. Between nets the station keeps
running in Modern Standby (it answered every 30 s with the screen off). The NPU is not used: with OpenVINO 2026.4 its Whisper pipeline refused the
station's callsign prompt and the large models did not finish compiling for it; at full speed the graphics did the
final text at 0.07x real time anyway. Set once on the laptop itself:
- the maker's performance profile to **Performance** (ASUS: MyASUS → Fan Profile; a quiet profile capped the whole
  processor to ~0.5 GHz);
- Settings → System → Power & battery → Presence sensing: **Turn off my screen when I leave** off;
- in your own plan, never **hibernate** while plugged in (a hibernated PC cannot start the station for a net).

## Using it, feature by feature

After installing, the station starts by itself whenever you sign in to Windows. Open the
**HamNet Station** shortcut on the desktop, or browse to `http://localhost:8091/` on the station PC,
or `http://<station-name>:8091/` from another PC or phone on your home network or Tailscale. On a phone, press
**Open on phone** at the top of the dashboard and point the phone's camera at the code.

The dashboard has one column per radio, with everything about that radio in it. The top of each column is
one set of tabs, **Radio** (meters, waterfall, frequency) | **Coach** |
**Stations heard**, all the same height, so the log always fills the space below
and never moves when you switch tabs. A dot on "Coach" means it has guidance (red: urgent).
On the Coach and Stations heard tabs, drag the bar between the tabs and the conversation to
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
- **Set level…** (on each radio card): sets the audio level just right, once per radio and cable. Step 1, *radio
  volume*: a handheld's amplifier adds hiss and hum as its volume goes up and distorts near the top, so for each knob
  position (1/8 … 7/8 of a turn) the station measures squelch-open hiss (after setting the Windows level) and the
  squelch-closed floor; the *margin* between them, and the hiss's crest factor (clean ≈ 10–11 dB, a clipping
  amplifier < 8), pick the cleanest position: the lowest one within 1.5 dB of the best undistorted margin. Step 2,
  *noise*:
  FM — open the squelch (MONI) for steady hiss (a data jack always has it); SSB/AM — a quiet frequency. The meter
  shows the level against a green target band (hiss -30 to -20 dBFS; band noise -45 to -32) with advice in words,
  the Windows **input volume** of that input (Windows 11: Settings → System → Sound → Input → the input → Input
  volume; same as the old panel's Levels → Microphone) with −/+ buttons, and **Adjust automatically**, which measures, sets
  the Windows level and measures again until the hiss is in the band (or says to turn the radio's volume, when
  Windows' range is not enough). Step 3, *voice*: close the squelch and let someone talk; peaks should stay
  between -12 and -3 dBFS. A handheld's speaker/earphone jack is very loud: set the radio's volume low and leave
  it, and let the Windows level do the fine adjustment (`station/levels.py`).
  When you retune a radio, press **Change** on its card and enter the new frequency and mode; every
  transmission after that is logged with it (kept across restarts in `data\operator.json`).
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
  talking; the final text replaces them about 2–3 seconds after they unkey. Callsigns are buttons (rest the
  mouse on one for its callbook card; click for the full lookup and the nets it takes part in), Q-codes
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
  the schedule); the rest need a Who?. A list of callsigns (net control reading back the check-ins), a question
  ("K7ABC, do I have it right?") or net control's welcome is never taken as the first station's own; two stations
  checking in together ("K7ABC and K7ABD") are the first one's. An answer closed with a call in a sentence of its own ("K7ABC. Welcome, great
  signal. K7NET.") is that station's (net control signing), and a check-in said twice still counts.
  **Who? suggests a voice** for a transmission that names nobody: "Sounds like K7ABC, the voice that said it at
  19:42 in this net", pre-filled, with ▶ buttons to compare the two recordings. Only voices of stations that stated
  their call (or that you set with Who?) earlier in the **same net** count, and only when the closest voice clearly
  beats the next one; otherwise it says so and suggests nothing. It is never written to the log or the ICS-309
  until you save it. Measured on FM nets: right 15 of 17 times (88 %) for about half of the transmissions whose
  voice was known in the net; across different days a voice is no better than chance, so it never looks there.
  Voiceprints (WeSpeaker ResNet34, 26 MB, CPU, 2 threads, transmissions of 2 s or more) are made in the background
  and kept in `data\voice\`; the installer downloads the model (`-SkipVoice` leaves it out; no model = no
  suggestions). `voice_hints: false` in `config\station.yaml` turns it off.
- **Correcting a line** (**✎ Fix** on any finished transmission, in the log and in a net's Minutes → Transcript):
  the words open in place, ready to edit, with ▶ to listen while you type. **Enter** (or Save) keeps it, **Esc**
  (or Cancel) leaves it. Your text then wins everywhere: the log, search, the minutes and attendance, the ICS-309 /
  ICS-213 and the Winlink messages (your text, then the accurate pass, then the live text), and the callsigns and
  speaker are read again from it. A corrected line shows **✎ corrected**; tap it to change it again or
  **Undo correction**. It is kept as an update line in the day's log (survives restarts, the accurate pass and a
  rebuild of the history), other open dashboards update at once, and it works for a net from any day.
  **Not speech** (in the same editor) marks noise, an open squelch or a station's tail: no words, no callsigns, no
  speaker; it leaves search, the minutes, attendance and stations heard, is not scored or learned from, and the
  line shows **· not speech** with **Undo**.
  - **Learned fixes** (Settings): when the same words are corrected the same way in two transmissions ("shelby
    ville" → Shelbyville), or once into a callsign, the station proposes it as a fix-up. Nothing changes until you
    press **Accept**: accepted ones go to `data\fixups_learned.yaml` (the `fixups:` format of `config/vocab.yaml`)
    and correct what the station hears from then on; **Ignore** hides a proposal, **Remove** takes one back out.
    The number on the Settings button is how many are waiting.
  - <a id="local-names"></a>**Local names** (Settings): your area's clubs, repeaters and web sites. A club whose
    name is said as a word ("SARC", said "sark") comes out of Whisper as an ordinary word: "the Sark Link repeater",
    "SarkNet", "go to SarkOnline". Each entry has the **name**, what it **means**, whether it is **said** as a word
    or letter by letter, how **Whisper writes it** (sound-alikes), an optional **web site** (said aloud: "Sark online
    dot com" → sarconline.com), **also heard** phrases (`Sarkowitz Repair = SARC Link repeater`) and, optionally, the
    **nets** it belongs to (by name; empty = every net). The station repairs the text in the log, search, the
    minutes and the Winlink messages (the heard words are kept, like the other fix-ups) and the ICS-213 lists the
    names said on that net under **Local terms** ("SARC: Springfield Amateur Radio Club (sarconline.com)"), for a
    reader outside the area. A plain word is changed only when it reads as a name: written with a capital
    mid-sentence, or with a radio word right beside it ("the sark net"); "we met at the park after the net" stays.
    Saving re-checks the nets of the last two days; an older day: `python -m tools.consensus <date>` with the
    station stopped. **Export area pack** saves the list (no callsigns, nothing personal) for other operators in
    your area; they **Import** it (a file or an https address) or put it beside `install.cmd`
    (`install.ps1 -AreaPack <file or address>` also works). Kept in `data\local_names.json`.
  - **Accuracy (from your corrections)** (in **Test transcription…**, under the test history): the word error rate
    of the live text, the accurate pass and the consensus text against everything you corrected, per radio, mode
    and net, with how many lines it is based on (under 20 is a rough guide; your eye starts from the text shown).
    The same from a terminal: `python -m tools.accuracy` (or `--data data` with the station stopped).
- **Coach** (per radio, the tab was "What you can do now" until 2026-11-09): step-by-step help and example phrases when someone asks for a relay,
  calls emergency or priority traffic, opens a net, takes check-ins or passes formal traffic, plus a
  reminder when your own 10-minute identification is due.
- **Net board** (per radio, a tab above the log): the net on the air on that radio, for net control and anyone
  logging it. Who is in, numbered in order (net control first), with name, town and time; **heard once** in grey
  italics (about half the calls the station hears only once are mis-heard: ask again); **traffic** on stations that
  said they have some ("with one piece of traffic"; "no traffic" is not); **Ask again** (the calls heard once);
  **Heard, not checked in** (named on the air, not acknowledged by net control); **Regulars not heard yet** (from
  the net's history); and **net control's ID timer**: when net control last said its call, amber at 9 minutes and
  red at 10 (97.119(a)). It refreshes after each transmission. **Review check-ins & minutes ›** opens the net's
  minutes; once the net has closed the button turns blue and says how many check-ins are left to review. The same
  attendance as the minutes, the ICS-309 and Winlink messages.
- **Review the check-ins** (Nets → the net's minutes → Attendance; or the Net board's button after the net): every
  call that took part, with ▶ (the transmission where the station heard it), **✓** (right), **✎** (the call is
  wrong: type the right one) and **✗** (not in this net). Calls heard once are marked **heard once** until you
  check them; the minutes text marks them **(?)**. ✎ and ✗ take the call out of the attendance (and the ICS-309 and
  Winlink messages); **undo** puts it back. Each mark is also kept in `data\review_log.jsonl` with what the station
  had (times heard, role): the first human truth for how well the station hears calls (before, every accuracy figure
  was an estimate).
- **Stations heard** (per radio, a tab above the log; the same calls as the log: your corrections, then the
  net-wide agreement, then the accurate pass, then the live text): one line per callsign (city, state, country · name, how
  often, and the date and time last heard — the most recently heard first); tap a line for the license, grid, zones and address (FCC, QRZ.com and the country file). Click any callsign for its card (with
  the nets that station takes part in: how many times and when last).
- **Net type** (a badge on each radio card): the kind of net being heard (ARES/RACES, SKYWARN, exercise, check-in, traffic, DX…),
  with a red banner if it sounds like a real emergency.
- **Search everything** (the box at the top): every transmission ever heard (live and accurate text), callsigns and
  callbook names, nets, and the minutes, in one list grouped as **Stations**, **Nets**, **Minutes** and
  **Transmissions**, with the words highlighted, ▶ to play and **Show in context ›** (today's log, or the net's
  minutes with the transmission lit up). Several words must all match; "quotes" for an exact phrase. Narrow it by net,
  radio, dates or callsign, and **Look in** nets and conversations, nets only, or conversations only (anything heard
  outside a net). **Search this log** on a radio's card opens it for that radio; **/** anywhere opens it; **Show more**
  pages past the newest 50. Searching 100,000 transmissions takes about a tenth of a second.
- **Nets** (on each radio card): the nets scheduled on that radio, the one running now in green, the next date of the
  others.
  A transmission belongs to a net only when the radio is on the net's frequency (the frequency on the radio's card —
  read by CAT, or what you entered with **Change** — is taken as what the radio is on). Off it, it is filed as a
  conversation, not as the net. A day already filed wrongly: `python -m tools.refile_nets YYYYMMDD --radio R --from
  HH:MM --to HH:MM` shows the moves, `--apply` writes them (station stopped).
- **Nets** (top of the page; **Nets…** on a radio card opens it for that radio). One window, full screen on a phone, with
  five tabs and a **Net** picker:
  - **Schedule**: one list of nets for this PC, each as a card in plain words ("Every 1st and 3rd Sunday at 19:00,
    146.940 MHz FM, 60 min"), its next date, net control and alert. **Each net is assigned to a radio**: click the
    radio on a net to move it to another (or to **No radio**: kept, not recorded). Every net says whether it will be
    recorded: **Records** (a CAT radio tunes itself, or the radio is already on the frequency), **You tune** (the alert
    asks you), **Clash** (the same radio has another net at that time: pick **Record this one when they meet**, or
    move one), **Can't hear** (the radio's band, mode or antenna can't receive it, or it is kept for Winlink) or **No
    radio**; the ones that need you are listed first, with one-click fixes (move to a radio that can, switch the
    antenna). A net that can't be heard, or loses a clash, is not listened to or tuned for. What a radio can hear
    comes from its model (2 m / 70 cm FM, IC-705, HF transceiver, or other: not checked; guessed from its name), its
    card's **Antenna: HF | VHF/UHF** (a one-socket multi-band radio such as the IC-705; HF at first) and **Used for:
    Nets | Winlink only**. Removing a radio in **Radios…** keeps its nets, with no radio. **Add a net** / **Edit**: name, radio, start time, minutes, frequency and
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
  - **Analysis** (per net; **Who takes part** on a net's card opens it): times met, stations on average, the **stations each time** chart (a table too), last
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
- **Nets on each radio**: each radio's column opens with its own nets: the one on the air and the next one
  (**N more today** / **Next 2 days** opens more, **This week…** opens Nets for that radio), each with its time,
  frequency and state (**on the air**, **You tune**, **Clash**, **Can't hear**, **not recorded** when it loses a clash).
  **Drag a net onto another radio's column** (a computer with a mouse): while dragging, every column says what would
  happen there (Records / You tune / Clash / Can't hear; a Winlink-only radio takes none), and on the drop you choose
  **Every time** or **Just Tue Nov 10** (that date only; the next time it is back on its own radio). **Undo** on the
  note that follows; when the radio can't hear it, the note offers to switch its antenna. Without a mouse (a phone),
  click a net in the column: the same menu moves it every time or just that date, or opens it in Nets. Above the
  columns, a red line counts the nets that can't be recorded (no radio, or the radio can't hear them).
- **HF scan** (a radio the station tunes by CAT, with its HF antenna on): **Scan Off / On / Auto** on the radio's
  card. **On** scans in the gaps between nets; **Auto** also scans while a scheduled net has been quiet for 3
  minutes. A net always comes first (scanning stops 5 minutes before one starts). Each pass picks a band open at this
  hour (night 80/40/160 m, morning 40/80/20, midday 20/17/15/40/12/10, late afternoon 20/40/17/15, evening 40/80/160/20)
  among the bands the antenna works on (**Inverted V…** on the card: its name and bands), tries frequencies
  remembered for this hour first, then sweeps the band's phone segment — cut to your license class's phone
  privileges (Settings → Station agent → license class; a General scans 3.800–4.000, 7.175–7.300, 14.225–14.350,
  21.275–21.450, and all of 160/17/12 m and 28.300–28.850) — in 3 kHz steps reading the S-meter, parks on
  anything about 2 S-units over the noise for 30 s, and stays while speech is transcribed there (until 2 minutes
  quiet). Frequencies with conversations are **remembered** (hours, days, count, calls heard; Pin or Hide them) in
  `data\scan_spots.json`. Turning the dial yourself pauses the scan for 30 minutes; when it stops, the radio goes
  back to where it was. Receive only: the only CAT commands are frequency, mode and the S-meter read.
- **Sidekick, the station agent (Draft mode)**: an optional AI operator's assistant. Its own two-sided card (set up; on a net) opens from **Settings → Station agent → Sidekick card**, from **Card** on Sidekick's card, or at `/help/sidekick`; the PDF is `docs/HamNet_Sidekick_Quick_Reference_Card.pdf`. Its on-air name is yours to
  choose (**Settings → Station agent → On-air name**, default **Sidekick**); a name that is a phonetic-alphabet word
  (Echo, Victor…), a number or a procedure word (Portable, Mobile, Break, Over) is refused, because it is said right
  after the call and listeners would hear it as part of the call. **Off by default**: the operator turns it on in
  **Settings → Station agent → Enable the station agent**; then each radio's card has an Off / Draft switch, also Off
  until chosen. Switch it to **Draft** on a radio's card: after each transmission (once the frequency has been quiet
  1.2 s) it decides whether your station should speak and drafts exactly what it would say and how long it would
  wait, shown at the top of that radio's column; **What would you say now?** asks on demand, 👍/👎 rates a draft,
  **▶ Hear it** plays the draft in Sidekick's voice in your browser. **It never transmits**: there is no transmit
  mode yet, and the CAT code still refuses PTT. (Each radio's Transmit (PTT) setup in Radios… is ready for it and
  switched off.)
  - **Its turn, read by the station.** Like an operator on a net, Sidekick has a turn only when net control opens
    check-ins to a station like yours, when someone calls it by call or name, or when it holds an answer and the floor
    opens. Outside those the station drafts "silent" itself, at once, without asking a model; when it asks, the model
    is offered only the moves that turn allows (replays of four real nets: the quick model, offered every move, tried
    to check in on 29 turns of one roll call).
  - **Send (says it on the air)**: a radio whose **Transmit** switch is on (Radios… → Transmit; off by default) gets a
    third position on its Sidekick switch, **Send**. A draft that would speak (and passed every check) is shown with
    **Sending in 3… Stop (Esc)**; the voice clip is made during the countdown. It is sent only while you are at the
    station: the dashboard must have seen your keyboard or mouse in the last 3 minutes (otherwise the card says
    "You are not at the station: Draft only"); Part 97 needs the control operator at the control point for a voice
    station that answers people. At key-up it checks again: still present, nobody transmitting (carrier, or the voice
    gate on HF), nothing new heard on that radio since the draft, the frequency known. PTT goes through the radio's
    transmitter (its time limit is the watchdog), with a short lead before the voice and a tail after it. **Esc, the
    Stop button or any click on the dashboard drops PTT at once.** The voice is your chosen Grok voice, or this PC's
    own Windows voice when there is no key or no internet. A transmission that was stopped, held or cut off does not
    count as said (the ID clock, the disclosure and check-ins move on only on a real send). **What would you say
    now?** never sends. Every send is logged in `data\agent_sends.jsonl`. Before a net: a test into a dummy load
    (Radios… → Transmit test).
  - **Who net control called** (2026-11-12, from a review of real nets where all three drafts that would have spoken
    were wrong turns): the station reads the calling sentence and the one before it (and the transmission before,
    when the call opens a transmission under 10 s after it). It does not check in when the call is for another kind of
    station ("Any station holding traffic, traffic only. Check in now."; maritime or mobiles only; members; EOCs),
    when net control says check-ins come later ("Before I start taking check-ins…"), or when only "Please call now."
    was heard and the start of the call was lost. **Roll calls by place** ("Stations Colorado, check in now"): it
    checks in only when the place is in **Settings → Station agent → Your region**; with no region set it never
    answers one. A net named after its state is not a roll call. **Emergency or priority traffic** ("break break",
    "emergency traffic") silences it on every net, flagged or not. In the net's own settings (Nets → the net →
    **Sidekick on this net**) a members-, mobiles- or traffic-only net can be set to **never check in**.
  - **Checked in, by what it hears**: when net control logs your call ("W9XYZ, thank you, you're in the log") or
    your own call is heard checking in (you on the air yourself, heard on another receiver), the radio counts as
    checked in to this net, even if the next station keys at once. Net control's "thank you" is not a question:
    Sidekick does not answer it. Your own transmission is never a call to answer.
  - **The card**: a **YOUR TURN** chip says where things stand (Check-ins open to you / CALLED: what was said /
    Answer ready / Checked in ✓ time and how / Listening: why not), with **I checked in myself** (or **Not checked
    in**), **Mute for this net** and **What would you say now?**. The last draft that would have spoken stays in
    view with its age and 👍/👎; the silent ones are counted, with the latest reason.
  - **No xAI key, or no internet**: the card says so in an amber band ("Offline rules: no xAI key on this PC…" with
    **Add a key**, or "Offline rules: xAI not reachable…"), and Sidekick keeps taking its turns with the offline
    rules below: it checks in when check-ins are open to it and acknowledges a call ("your control operator will
    answer you personally"); it cannot answer questions or think. Nothing waits for the network. With a key, the
    station checks every 2 minutes that xAI answers (one free request, the model list), so the card says "not
    reachable" before a turn needs it. **▶ Hear it** without a key or internet uses this computer's own voice
    (the browser's), heard only there.
  - **Two speeds.** The **model** (Grok 4.20 fast by default: measured 1.0 s median on real nets) decides the turn:
    check-ins, acknowledgements, short answers. A question that needs thinking gets a short "copy your question, working on
    it, I'll report back" and goes to the **thinking model** (Grok 4.5 by default) while the net carries on. When the
    answer is ready Sidekick waits for its place: when net control next asks for comments, questions or further
    business it says "This is W9XYZ Sidekick, I have a response ready for the earlier inquiry from K7ABC"; told to go
    ahead, it reads the prepared answer word for word (the code puts the prepared words in; at most 70 words, checked
    against the FCC rules before it is ever offered). Outside a net it offers the answer once the frequency is open
    after the station that asked. Up to three questions are worked on at once and answered in the order asked (the card shows the next ones);
    if the thinking fails it says so at the next opening and that you will follow up. **Outside a net** (a conversation) a
    ready answer is given at a pause, not announced: after 3.5 s of quiet, when a station turns it over, or when someone
    asks for it ("Sidekick, what did you find?"): "This is W9XYZ Sidekick, for K7ABC: …". The card shows the answer's state (working, ready, announced, failed).
  - **Offline it keeps working, nothing waits.** When the model cannot be reached (no internet, a timeout of 8 s)
    fixed rules write the draft at once: check in when net control opens check-ins to a station like yours (also at
    "next station, please" during open check-ins; not when the call is for maritime, mobile, city or county stations,
    EOCs or members), acknowledge when called by call or name ("your control operator will answer"), and announce
    and read an answer already prepared. The model is not tried again for 30 s to 5 minutes. Past 240 model calls an
    hour the rules draft too.
  - It remembers per radio, across a restart (`data\agent_state.json`), when it last identified, whether it has
    disclosed itself and checked in to the current net, and a prepared answer, so a restart mid-net never checks in
    twice. Local names (Settings → Local names) reach it repaired and explained, with the local and UTC time.
  - Every draft is checked in code against FCC Part 97 (the model cannot talk its way past them): your call at the
    end of each exchange and every 10 minutes, spoken in English or phonetics, never an SSID (97.119); on its first
    transmission in a contact it says it is an automated assistant and names you as control operator, and it never
    claims to be a person (97.113(a)(4)); no music, indecent language or codes (97.113(a)(4)); no business
    (97.113(a)(2),(3)); no relayed messages (97.115); silent during emergency or priority traffic unless net control
    calls it, and while anyone transmits (97.101); under about 30 seconds; inside your license class's phone
    privileges (97.301/97.305); and the answer steps (no "response ready" without one, the prepared words only). A
    draft that fails any check is shown struck through, **Would not send**, with the rule. Settings → Station agent
    holds the name, your first name, your check-in line, license class, the model, the thinking model and up to two
    models to compare on the same traffic. Drafts are kept in `data\agent_drafts.jsonl` (never committed).
  - **Replay a real net**: `python -m tools.sidekick_replay 20261110 --radio net --from 19:30 --to 20:30` (on the
    station; `--model rules` = the offline rules only; `--deep` also runs the thinking model) prints every draft it
    would have sent after the transmission it followed, every draft the checks stopped, and the model's latency.
- **Sidekick's voice** (Settings → Sidekick's voice): xAI text-to-speech, **heard only in your browser, never on a
  radio** — ▶ on each of xAI's voices plays the same audition (a check-in, "working on it", "I have a response ready",
  the answer); choose one and a speed (0.7–1.5), saved in the station's settings; **▶ Hear it** on a draft uses it.
  Your call is spoken letter by letter. Each clip is made once (xAI bills by characters) and kept in `data\tts\`;
  offline only the clips already made play.
- **Grok speech-to-text, compared** (Settings → Grok speech-to-text, off by default): on the radios you tick, each
  transmission's recording is also sent to xAI's speech-to-text as it is heard (your xAI key; hinted with your call,
  your local names and the net's name), and kept beside the station's own text (`data\grok_stt\<day>.jsonl`). The
  station's engine stays the transcriber. **Never queued**: a transmission heard while xAI is unreachable, while two
  requests are out, past the day's minutes (default 60) or longer than 2 minutes is skipped and counted. **Compare
  today** shows word errors and calls found for each engine against your corrections (human) and the accurate pass
  (large-v3), and each transmission side by side. A past net: `python -m tools.grok_compare 20261110 --radio net
  --from 21:00 --to 22:00 --limit 60`. The recordings leave the PC; xAI had not published a per-minute price
  (2026-11-10), so the minutes cap bounds the spend.
- **Quiet HF nets**: on a radio the station tunes (CAT), when an HF or 6 m net has been silent for 3 minutes (nothing
  heard in the last 3, also after a net ends early) and another net is scheduled at the same time — a net that lost
  a clash included — the station switches to that one (it goes back only if everything else
  is quiet too). Each net remembers whether it was heard; a net **not heard 3 times in a row** is marked in its
  radio's column and in Nets, with **Remove** and **Keep it** (keeping starts the count again).
- **Nets today** (older layout, replaced by the columns above): a strip under the header lists, one line each, the nets still to come today and the one on the
  air (time, name, frequency, radio). The next one is highlighted; one on the air shows **on the air**, or
  **radio paused** when its radio is not recording. Tap a line to open it in Nets. Folded by default to the net on
  the air and the next one (**N more today** opens the rest, in a short scrolling list; the choice is remembered in
  that browser). A radio card's **Nets:** line names its next two nets and **N more…** opens the rest in Nets.
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
  connect. Close Winlink Express first: while it is open the dialog says so and the button stays off (checked
  every 2 seconds; it turns on as soon as Express is closed), and a refused write is logged. The station backs up
  Winlink's message list before each write.
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
  A licence more than 300 km away (Settings → VHF/UHF reach; raise it for a wider linked system) that nothing backs (heard in one transmission, the live and accurate passes
  did not read it alike, no name, town, earlier check-in or roster entry) is almost always a misreading (11-03:
  clean roll-call check-ins ran to 294 km on the linked system; past 400 km only 5 of 59 licences were read alike
  by both passes, against 58% of local stations). Being said once is not held against a call: a roll call hears
  most stations once. It stays in the text, shown faded and struck through, and is left out of the station
  counts, Stations heard, attendance and the Winlink messages. ✎ Fix puts it back if it was really there.
  **A far call one letter from a near one** is set aside the same way even when both passes read it alike or it was
  said twice (the same audio is misheard the same way: 2026-11-10, "W7XBD", 944 km, for W7XCD 8 km away), when a
  licence within 50 km is one rhyming letter from it; the one-rhyme licences within 150 km are offered next to it,
  nearest first ("W7XBD? W7XCD W7XGD W7XZD"), one click to look each up. It is never moved on its own: a visitor
  on a link can be one letter from a local call (2.2% of far calls are, measured), so you choose. A name, town,
  earlier check-in or roster entry for the far call still counts it.
  **A net's own reach:** an intertie or linked system carries stations far past one repeater (a statewide intertie,
  11-10: check-ins from two neighbouring states, 440-590 km). Set **Reach (km)** on that net (Schedule → edit the net); it
  replaces the station's VHF/UHF reach for that net only. A PO-box ZIP (2% of licences) has no centre of its own:
  its 3-digit postal region places it, but only to say *far* (beyond the reach + 200 km); nearer it stays unknown.
  A licence never heard needs a name, a town or a history
  behind it, and a call nothing supports stays as heard, marked *unverified*.
  **Confirmed or not.** Every call is marked: *confirmed* by an Active FCC licence, a current QRZ.com record or the
  roster, or *can't be confirmed* (a dashed chip with a "?", the reason in its tooltip and on its Stations heard
  card: "FCC: licence cancelled; not on QRZ.com"). An unconfirmed call is still counted. The station looks up on
  QRZ.com every call the FCC does not show Active (cached; "not found" remembered a week; nothing when offline or
  without a login, and the reason then says "not checked"). When QRZ answers with another call that lists the
  heard one as an alias, the heard call is that operator's old call and moves to the current one ("QRZ.com: KE7ABC
  is an earlier call of K7ABC"); on VHF/UHF only when the current licence is within reach (11-03 nets: 4 old calls
  moved, all within 124 km; 2 left as heard, their operators 877 and 1055 km away). The transmission keeps its heard text; its
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
  2 minutes (`final_after_min`) it sends the **whole net** in 4-minute blocks (30 s of context each side) for the
  *final* accurate text, which replaces the provisional one. Whisper hears whole turns instead of 1–3 s pieces, so
  the final text gets far more right (see "How the accurate pass works" below). Anything missed (home server
  unreachable) is caught up 15 quiet minutes after the net; **Run now** on a net's header starts that at once (**Try now** when the home server was unreachable). The header shows what it is doing: how many are done, a progress bar
  and about how long is left, then **✓ Accurate text for all N**. Corrected transmissions show the new text,
  with the live text a click away. The Winlink window says the same thing: send now with the live text, or
  wait; when the pass finishes it offers **Load the accurate text** (the forms then use it). If the home server is
  unreachable the header says so and the station tries again every 2 minutes. The upload key goes in
  `config\secrets.env` as `EMCOMM_STT_KEY=…` (kept across updates, never in git). Transmissions from before
  yesterday (no longer in memory) can be run by id: `POST /api/accurate {"ids": ["…"]}` (up to 50; loaded from
  the history index, kept off the live feed).
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
  automatically: `C:\RMS Express` and any other install) and which callsign account sends; **Net alerts**: the chime, and
  notifications on the station PC; **VHF/UHF reach, km** (300 by default: calls licensed farther away that nothing
  else confirms are not counted; raise it for a linked repeater system); **Learned fixes** (your ✎ Fix corrections).

Which radios exist (and their sound inputs) and the first nets are set in `C:\ham-net-speech-to-text\config\station.yaml` (every option is explained in
`config\station.example.yaml`). After editing it, restart the station: Task Scheduler → **HamNet Station** →
End, then Run (or sign out and back in; after End it also comes back by itself within 2 minutes). Everything heard is kept in `data\logs\<date>.jsonl` and
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

