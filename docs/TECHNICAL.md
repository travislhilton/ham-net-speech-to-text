# HamNet Station — how it works inside

For developers and the curious. Operators: the [README](../README.md) and the [manual](MANUAL.md).

## Segmenting

How the station turns a radio's audio into transmissions (`station/segmenter.py`; per-radio settings in
`config/station.yaml`, defaults per gate in `Station.SEG_DEFAULTS`):

| Setting | FM carrier gate | HF voice gate | What it does |
|---|---|---|---|
| `preroll_ms` | 500 | 600 | audio kept from before the gate opened; on HF the voice gate hears a first syllable late |
| `squelch_db`, `squelch_back_ms` | off | -70 dB, 2500 | squelched audio (a handheld behind its own squelch) is digital silence until the squelch opens: the pre-roll reaches back to where that silence ended, up to 2.5 s. The voice gate fired 0.6-1.5 s into a call sign on a squelched FM roll call (2026-11-10: 60% of the clips began mid-word, "7XYZ" for a KG7XYZ). HF audio is never that silent, so it keeps the fixed pre-roll |
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
   each word back to the transmission it was spoken in (by the time the word ENDS: Whisper on OpenVINO stretches the
   start of the first word after a pause back into the silence, which moved first words a turn early — a real QSO
   through a voice gate: 53 → 31 wrong first/last words; at a pause split the boundary sits 0.4 s early
   because Whisper's word times run early).
5. Per transmission: text, word times, mean word confidence and flags. Empty, a lone "Thank you."-style phrase
   with low confidence, or confidence under 0.3 → the station keeps the live text (`accurate_echo`).

Stages: **provisional** = each transmission with the ~25 s before it, sent as it ends (median 0.8 s on the Mac
Studio, ~3 s for a silent clip); **final** = the whole net in 240 s blocks + 30 s of context each side, when the net
has been quiet 2 minutes (~11x real time: a 1-hour net in ~6 minutes). A late provisional never overwrites a final.

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
  GPU or CPU (not the NPU: large-v3 did not compile for it; model `whisper-large-v3-int8-ov`, 1.5 GB, downloaded by the installer), or faster-whisper
  (CTranslate2) on an NVIDIA card. Whisper's 30 s long-form walk is done one window at a time by the station's own
  code, so the pass can stop between windows. OpenVINO gives word *times* but no word *probabilities*: the merge of
  the two decodes then prefers the words farther from a window cut, a lone "Thank you."-style text is always set
  aside, and there is no low-confidence test (measured on the home server with the probabilities removed: 2–4 points more
  word errors, see below).
- **What the PC can sustain** (`tools/pick_accurate.py`, run by the installer; re-run after a hardware change):
  each model on each device decodes a ~55 s test block; rtf = seconds of work per second of audio. Large-v3 if it
  runs at rtf ≤ 1.0 (the final text of a net then arrives within ~2.5× its speaking time), else turbo, else none; the
  fastest device — not the CPU just because the live text is on the GPU (measured below: the CPU slowed the live
  text more than sharing the GPU did); **both stages** only at rtf ≤ 0.12 (a provisional text for every transmission), otherwise **final text
  only**. Written to `config\engine.yaml` (`accurate:`). Final text only does not mean waiting for the end of a long
  net: every 5 minutes (`rolling_min`; 0 = only after the net) the finished part of the net — transmissions that
  ended at least 45 s ago — gets its final text, with the rest of the net so far as context; the pass after the
  net then does only what is left.
- **The live text always wins**: the pass runs in its own process (`python -m station.accurate_local`) at
  below-normal priority, with its own model (never the live engine's), on the GPU at low queue priority. It is held
  while any radio is receiving or a live final is queued or being transcribed, and for 2 s after (`quiet_s`); it
  stops between 30 s windows, so at most one window (seconds) overlaps a new transmission. The model is loaded for
  the first job and the process ended after 10 idle minutes (`unload_after_min`), giving the memory back. On a
  laptop on battery it does not run unless `local_on_battery: true`.
- Settings (`config\station.yaml`, `accurate:`): `local: auto | final_only | off`, `local_on_battery`, `quiet_s`,
  `unload_after_min`, `rolling_min`.

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

**2026-11-06, word probabilities on this PC:** OpenVINO's Whisper gives none, and the merge of the final pass's two
decodes is decided by them. The pass now reads them from the same model in a second, teacher-forced step
(`transcribe/ovscore.py`), and a stretch that only repeats itself never wins the merge (Whisper rates its own loops as
very likely). Six synthetic nets (4 FM + 2 SSB, the station's own loop guard applied, word errors / callsigns found):

| | word errors | callsigns found | callsigns right |
|---|---|---|---|
| home server (mlx large-v3) | 54.3 % | 18.9 % | 48.6 % |
| laptop, Core Ultra 7 (Arc GPU), large-v3 int8, before | 55.8 % | 20.5 % | 50.6 % |
| **laptop, with word probabilities** | **53.9 %** | **23.2 %** | **52.4 %** |
| i5-12600H (Iris Xe), before | 56.4 % | 18.4 % | 49.3 % |
| **i5-12600H, with word probabilities** | **54.7 %** | **20.0 %** | **51.4 %** |

The cost is memory, not time: the model is compiled twice (peak 6.7 GB vs 4.1 GB for large-v3 int8); on the laptop
no slower, on the i5 ~13 % slower. A PC with room for the model but not for the second copy runs the pass without
them (never waits). The home server's text is unchanged by the loop rule.

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

## Sidekick's knowledge library

`station/knowledge.py` gives Sidekick material to answer questions and give prepared talks from ("present this
project and how to use it", "explain our EOC's Winlink setup"), with the source of every piece, instead of guessing.

**Where it lives** (all under the data folder, which git ignores; nothing in it is ever committed):

| Path | What |
|---|---|
| `data\knowledge\` | the operator's own files: `.md .txt .html .pdf .docx .csv`, subfolders too; copy files in or upload them |
| `data\knowledge\notes\` | short notes with a title, one `.md` each (first line `Title: …`) |
| `data\knowledge\web\` | pages fetched once from a URL (first line `URL: …`); fetched again only on demand |
| `data\knowledge\index.db` | the search index (SQLite FTS5, bm25 ranking, porter stemming); a cache: delete it and it is rebuilt |

Built in, from the install folder: `README.md`, `docs/MANUAL.md`, `docs/TECHNICAL.md`, `docs/ARCHITECTURE.md`,
`station/static/card.html` and `station/static/sidekick_card.html`, re-indexed whenever one changes (an update).
They cannot be removed.

**Indexing.** Text is cut at headings (and at a manual-style `- **Feature**` bullet, so one long feature list is not
one section), then at paragraphs and lines, into chunks of about 800–1200 characters with a 150-character overlap;
each chunk keeps its source, its section heading and a stable id (a hash of all three). A file whose size and
modification time have not changed is not read again; one whose bytes have not changed is not chunked again. A file
that cannot be read (a damaged document, a scanned PDF with no text, `pypdf` missing) is listed with the reason and
does not stop the rest. PDFs need `pypdf` (in `requirements-windows.txt`); `.docx` is read with `python-docx` if it
is installed, otherwise straight from the file's `word/document.xml`. Limits: 20 MB a file, 200 MB for the operator's
files, notes and pages together (the built-in documents do not count), 2 MB and 15 s for a web page, which must be
`text/html` or `text/plain`. Without FTS5 in the PC's SQLite (the Windows Python 3.12 build has it) a slower LIKE
search with a tf-idf score is used instead.

**Questions from speech.** A question is lower-cased, spoken filler and small words are dropped ("um, so, how do
I…"), spelled letters are joined ("E O C" → eoc), ham abbreviations are kept as words and a few get their long form
too (EOC, ARES, RACES, RMS, CW → morse, PTT), and the remaining terms are searched with OR so a chunk need not hold
every word; bm25 ranks chunks with more of the rarer terms first, a match in a section heading counting double.

```python
lib = Library(data_dir, install_dir)
lib.search("how do I set the audio level", k=6)   # [{id, source, section, text, score, kind, name}]
lib.context_for(question, budget_chars=6000)      # best chunks under [name › section] labels, for a prompt
lib.outline(topic, budget_chars=12000)            # more chunks, grouped by source in document order: a talk
lib.add_note(title, text); lib.add_url(url); lib.add_file(name, data); lib.remove(source); lib.list()
```

## Home-network server (optional)

When the station is on the home network it can upload each transmission to the home server
(`transcribe/`, FastAPI + mlx-whisper) for the accurate pass, which stores nets and transmissions in
PostgreSQL; `analyze/` uses LLMs on an optional OpenAI-compatible LLM server for structuring and summaries, and falls back to
rules when it is unreachable. `python -m tools.net start|stop|status` opens and closes a net.
Details: `docs/ARCHITECTURE.md`, deploy: `deploy/my-server/`.

**The server holds the GPU only while nets need it** (`transcribe/gpu.py`). Whisper loads on the first request
(about 2 s from the local model cache), the GPU memory a decode frees is handed back after every request, and after
`STT_IDLE_UNLOAD_S` seconds with no request (default 600; 0 keeps it loaded) the model is unloaded and the service
returns to well under 1 GB. `GET /health` shows `whisper_loaded`, `idle_s` and the GPU memory in use. `STT_MLX_CACHE_GB`
(default 4) caps what MLX may keep between decodes. Before this the cache was never returned: a server with no
restart grew to 43 GB in 12 hours of final passes; six replayed final passes now stay at 4.5 GB, no slower.

## VHF/UHF path analysis (`hamkit/pathprofile.py`)

Sidekick is asked things like "can the EOC reach the shelters on 2 m, and how should we set up Winlink?". It must
not guess terrain, so it calls this module and treats its numbers as the answer.

- **Terrain**: the great-circle path is sampled every `step_m` (100 m) up to `max_samples` (600; longer paths are
  sampled more coarsely). `source="usgs"` (default) asks the USGS Elevation Point Query Service (3DEP, US only, one
  point per query, 4 at a time); `source="open-meteo"` asks Open-Meteo (Copernicus GLO-90, worldwide, 100 points
  per query, one query at a time; 90 m cells round off narrow ridges a little). Answers are cached in
  `elevation.sqlite` in the callbook folder, keyed by source and lat/lon to 4 decimals. If the chosen source cannot
  answer for any point the call raises `ElevationError`: there is no fallback to another source or to flat ground.
  `elevation_fn(lat, lon)` replaces both (tests).
- **Geometry**: effective earth radius `k` = 4/3, earth bulge d1·d2/(2kR) added to the ground, a straight ray from
  antenna to antenna, the first Fresnel zone radius √(λ·d1·d2/d). The verdict comes from the smallest clearance
  ratio along the path: ≥ 60% = clear, ≥ 0 = line of sight but grazing / partly blocked, < 0 = obstructed (by the
  curve of the earth when the path would clear on a flat earth).
- **Loss**: free space 32.44 + 20·log10(d km) + 20·log10(f MHz), plus ITU-R P.526 knife-edge loss J(v). Several
  edges: Deygout, the main edge and then the worst one on each side of it (`max_edges` 3); a side edge counts only
  when it really blocks its half of the path, since on smooth ground every sample is a near-grazing edge and adding
  them all counted the same ground two or three times. `clutter_db` allows for buildings and trees, which the terrain
  does not include (5–20 dB at VHF/UHF).
- **Link budget**: watts, cable loss at each end, antenna gains (per site via `Point.gain_dbi`), sensitivity for FM
  voice (−120 dBm, 12 dB SINAD) and VARA FM / 1200-baud packet (−110 dBm); a margin of `fade_margin_db` (10 dB) is
  "reliable", 0–10 "marginal", below 0 "not expected".
- **Calls**: `analyze(a, b, mhz)` (dict + `text`; `profile=True` adds up to ~200 rows for a chart),
  `matrix(points)` (every pair), `best_relay(points, candidates, mode="vara_fm")` (ranked by sites reached with the
  wanted margin, then by the weakest margin), `height_needed(a, b, at="a")` (lowest mast for 60% clearance, `None`
  over 30 m). Points are `"lat,lon"`, a Maidenhead grid (its centre: give coordinates for real work), a tuple, a dict
  or a `Point`.
- **CLI**: `python -m hamkit.pathprofile 40.0150,-105.2705 39.9580,-105.2950 --mhz 146 --ha 10 --hb 6 --watts 50
  [--ft] [--source open-meteo] [--json [--profile]]`.
- **Not modelled**: buildings, trees and indoor loss (only as `clutter_db`), ducting and troposcatter, multipath,
  repeater coverage and antenna patterns (antennas are assumed aimed at each other). Deygout with three edges can
  still be pessimistic over long rolling paths; treat a few dB either way as noise.
