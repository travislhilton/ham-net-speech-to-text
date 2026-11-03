"""Synthetic net benchmark with known text.

A net script (net control + check-ins, callsigns from real Active FCC licences in the region, spelled phonetically,
as letters, or mixed) -> macOS `say` per phrase (one voice per station) -> a timeline with realistic pauses ->
a radio channel (FM repeater through a data jack, or HF SSB) -> cut by the station's own Segmenter (carrier gate +
0.8 s pause splits for FM; a voice gate for SSB) -> per-transmission WAVs + a station-style log + the truth.

    python gen.py OUTDIR KIND SEED [N_STATIONS]      KIND = fm | ssb_hard | ssb_mod
"""
import json, os, random, subprocess, sys, tempfile
from datetime import datetime
import numpy as np, soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from station.segmenter import Segmenter

SR = 16000


def people_from_fcc(n=3000):
    """Real Active licences in the Mountain West (deterministic sample; the script shuffles with its seed)."""
    import sqlite3
    from hamkit.fcc import DEFAULT_DB
    con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    rows = con.execute("""select hd.call, en.first, en.city, en.state from hd join en on en.usi=hd.usi
        where hd.status='A' and en.state in ('UT','ID','WY','NV','CO','AZ','OR','WA','MT') and en.first != ''
        and hd.usi % 97 = 13 order by hd.usi limit ?""", (n,)).fetchall()
    return [{"call": c, "first": f.title(), "city": ci.title(), "state": s} for c, f, ci, s in rows if c and 4 <= len(c) <= 6]
VOICES = ["Samantha (English (US))", "Daniel (English (UK))", "Eddy (English (US))", "Flo (English (US))", "Fred",
          "Grandpa (English (US))", "Grandma (English (US))", "Kathy", "Ralph", "Reed (English (US))",
          "Rocko (English (US))", "Sandy (English (US))", "Shelley (English (US))", "Karen", "Moira (English (Ireland))",
          "Rishi", "Tessa", "Junior", "Albert", "Reed (English (UK))", "Eddy (English (UK))", "Flo (English (UK))"]
PH = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet Kilo Lima "
              "Mike November Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey X-ray Yankee Zulu".split()))
DIG = dict(zip("0123456789", "zero one two three four five six seven eight niner".split()))
DIG_PLAIN = dict(zip("0123456789", "zero one two three four five six seven eight nine".split()))


def spoken(call, how):
    if how == "phon":
        return " ".join(PH.get(c) or DIG[c] for c in call)
    if how == "letters":
        return " ".join(c if c.isalpha() else DIG_PLAIN[c] for c in call)
    # mixed: prefix letters plain, suffix phonetic ("K K seven Charlie Uniform Sierra")
    i = next(k for k, c in enumerate(call) if c.isdigit())
    return " ".join([c for c in call[:i]] + [DIG_PLAIN[call[i]]] + [PH.get(c) or DIG[c] for c in call[i + 1:]])


def tts(text, voice, rate, cache={}):
    key = (text, voice, rate)
    if key in cache:
        return cache[key]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        p = f.name
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", p, "--file-format=WAVE", "--data-format=LEI16@16000", text],
                   check=True)
    x, sr = sf.read(p, dtype="float32"); os.unlink(p)
    assert sr == SR
    # trim TTS leading/trailing silence
    nz = np.flatnonzero(np.abs(x) > 0.01)
    x = x[max(0, nz[0] - 160): nz[-1] + 160] if len(nz) else x
    cache[key] = x
    return x


# ---------------------------------------------------------------- the script
def script(rng, kind, n):
    people = people_from_fcc()
    rng.shuffle(people)
    ncs, stations = people[0], people[1:n + 1]
    voices = VOICES[:]; rng.shuffle(voices)
    spk = {p["call"]: {"voice": voices[i % len(voices)], "rate": rng.randint(160, 205),
                       "how": rng.choices(["phon", "letters", "mixed"], [0.55, 0.3, 0.15])[0],
                       "snr": None} for i, p in enumerate([ncs] + stations)}
    S = lambda p: spoken(p["call"], spk[p["call"]]["how"])
    nm = "Prairie" if kind == "fm" else "Rocky Mountain"
    tx = []  # (speaker call, [(spoken phrase, reference phrase)])
    def T(p, *phr):
        tx.append((p["call"], [x if isinstance(x, tuple) else (x, x) for x in phr]))
    sc = lambda p: (S(p), p["call"])          # (spoken, reference) for a call
    def cat(*parts):                           # join (spoken, ref) parts and plain strings
        sp = " ".join(x[0] if isinstance(x, tuple) else x for x in parts)
        rf = " ".join(x[1] if isinstance(x, tuple) else x for x in parts)
        return (sp.replace(" ,", ","), rf.replace(" ,", ","))
    T(ncs, cat("Good evening, this is", sc(ncs), ",", ncs["first"], ", net control for the", nm, "check-in net."),
      "This is a directed net. Please hold your traffic until called.",
      "Is there any emergency or priority traffic?")
    T(ncs, "Hearing none, we will start with check-ins from the list.")
    late = []
    for i, p in enumerate(stations):
        a = rng.random()
        if kind == "fm":
            T(ncs, cat("Next on the list is", (p["call"], p["call"]) if rng.random() < .5 else sc(p), ",", p["first"], "in", p["city"], "."))
        else:
            T(ncs, cat("Next is", sc(p), "."))
        if a < 0.12:
            T(ncs, cat("Nothing heard from", (p["call"], p["call"]), ", moving on."))
            if rng.random() < .5:
                late.append(p)
            continue
        form = rng.random()
        if form < .4:
            T(p, cat(sc(p), ",", p["first"], ",", p["city"], ", no traffic."))
        elif form < .7:
            T(p, cat("This is", sc(p), ",", p["first"], "in", p["city"], ","), "checking in with no traffic, over.")
        else:
            T(p, cat("Good evening", ncs["first"], ".", "This is", sc(p), "."),
              cat(p["first"], "in", p["city"], "."),
              rng.choice(["Nothing for the net tonight.", "No traffic, thanks for running the net.",
                          "I have one item for the net when you are ready.", "Good signal into " + p["city"] + " tonight."]))
        acks = [cat((p["call"], p["call"]), ",", p["first"], ", good signal, got you down."),
                cat("Thank you", p["first"], ", you are in the log."),
                cat("Roger", (p["call"], p["call"]), ", thanks for checking in."),
                cat("Good evening", p["first"], ".", "Got you down from", p["city"], ".")]
        T(ncs, rng.choice(acks))
    T(ncs, "Are there any late or missed check-ins?")
    for p in late:
        T(p, cat(sc(p), ",", p["first"], ", late check-in, no traffic."))
        T(ncs, cat("Roger", (p["call"], p["call"]), ", you are in the log."))
    T(ncs, cat("Hearing no further check-ins, this is", sc(ncs), "closing the net."), "Thanks everyone. Seventy three.")
    return ncs, stations, spk, tx


# ---------------------------------------------------------------- channels
def bandpass(x, lo, hi, sr=SR, soft=50):
    f = np.fft.rfftfreq(len(x), 1 / sr)
    g = np.clip((f - (lo - soft)) / (2 * soft), 0, 1) * np.clip(((hi + soft) - f) / (2 * soft), 0, 1)
    return np.fft.irfft(np.fft.rfft(x) * g, len(x)).astype(np.float32)


def tilt(x, db_per_oct, f0, sr=SR):
    f = np.fft.rfftfreq(len(x), 1 / sr)
    g = 10 ** (db_per_oct * np.log2(np.maximum(f, f0) / f0) / 20)
    return np.fft.irfft(np.fft.rfft(x) * g, len(x)).astype(np.float32)


def freq_shift(x, hz, sr=SR):
    n = len(x); X = np.fft.fft(x); h = np.zeros(n); h[0] = 1
    h[1:(n + 1) // 2] = 2
    if n % 2 == 0:
        h[n // 2] = 1
    a = np.fft.ifft(X * h)
    return np.real(a * np.exp(2j * np.pi * hz * np.arange(n) / sr)).astype(np.float32)


def slow_noise(rng, n, bw_hz, sr=SR):
    """complex gaussian low-passed to bw_hz: Rayleigh fading envelope source"""
    k = max(2, int(n * bw_hz / sr))
    c = rng.normal(size=k) + 1j * rng.normal(size=k)
    t = np.linspace(0, k - 1, n)
    return np.interp(t, np.arange(k), c.real) + 1j * np.interp(t, np.arange(k), c.imag)


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


def build(rng, kind, spk, tx):
    """Timeline: returns audio, gate bool per 32 ms frame, phrase spans [(start_s, end_s, ref text, tx index)]."""
    np_rng = np.random.default_rng(rng.randint(0, 2 ** 31))
    segs, spans, carrier = [], [], []     # segs: list of arrays; carrier: list of (start_s, end_s)
    t = 2.0
    audio = [np.zeros(int(2.0 * SR), np.float32)]
    cur = 2.0
    for k, (call, phrases) in enumerate(tx):
        s = spk[call]
        if s["snr"] is None:
            if kind == "fm":     # bimodal like 442.100: full-quieting locals and noisy fringe stations
                s["snr"] = rng.choice([rng.uniform(16, 24), rng.uniform(0, 6), rng.uniform(3, 9)])
                s["mobile"] = rng.random() < .3
                if k == 0:           # net control: a mid-quality signal into the repeater
                    s["snr"] = rng.uniform(4, 14)
            elif kind == "ssb_hard":
                s["snr"] = rng.uniform(-4, 4)
                s["shift"] = rng.uniform(-70, 70)
            else:
                s["snr"] = rng.uniform(18, 32)
                s["shift"] = rng.uniform(-40, 40)
        # gap before this transmission (turn-taking)
        gap = rng.uniform(0.5, 2.2) if rng.random() < .8 else rng.uniform(2.5, 6)
        audio.append(np.zeros(int(gap * SR), np.float32)); cur += gap
        keyup = rng.uniform(0.15, 0.4)
        audio.append(np.zeros(int(keyup * SR), np.float32))
        tx_start = cur; cur += keyup
        parts = []
        for j, (sp, ref) in enumerate(phrases):
            # a speaker's phrases: split into sub-phrases at commas sometimes, with human pauses between
            chunks = [(sp, ref)]
            if rng.random() < .35 and "," in sp and sp.count(",") == ref.count(","):
                a, b = sp.split(","), ref.split(",")
                cut = rng.randint(1, len(a) - 1)
                chunks = [(",".join(a[:cut]) + ",", ",".join(b[:cut]) + ","), (",".join(a[cut:]).strip(), ",".join(b[cut:]).strip())]
            for (csp, cref) in chunks:
                if not csp.strip(" ,."):
                    continue
                x = tts(csp, s["voice"], s["rate"])
                spans.append((cur, cur + len(x) / SR, cref, k))
                parts.append(x); cur += len(x) / SR
                p = rng.uniform(0.15, 0.6) if rng.random() < .65 else rng.uniform(0.8, 1.6)
                parts.append(np.zeros(int(p * SR), np.float32)); cur += p
        parts.pop(); cur -= 0  # keep last pause as the unkey tail
        tail = rng.uniform(0.1, 0.3)
        parts.append(np.zeros(int(tail * SR), np.float32)); cur += tail
        seg = np.concatenate(parts)
        seg = channel(np_rng, kind, s, seg)
        audio.append(seg)
        # fix cur drift: recompute from sample counts
        cur = sum(len(a) for a in audio) / SR
        carrier.append((tx_start, cur, s["snr"]))
    audio.append(np.zeros(int(3 * SR), np.float32))
    x = np.concatenate(audio)
    return finish(np_rng, kind, x, carrier), [(a, b) for a, b, _ in carrier], spans


def finish(rng, kind, x, carrier):
    """Noise that belongs to the path, not the speaker: FM = each user's own noise while they transmit (their
    signal into the repeater) + the repeater's quiet hang carrier; SSB = constant band noise + crashes + AGC."""
    n = len(x)
    if kind == "fm":
        noise = tilt(rng.normal(size=n).astype(np.float32), -2.0, 800)
        noise /= rms(noise)
        lvl = np.full(n, 0.1 * 10 ** (-24 / 20), np.float32)              # repeater hang carrier: quiet
        for a, b, snr in carrier:
            lvl[int(a * SR):int(b * SR)] = 0.1 * 10 ** (-snr / 20)
        y = x + noise * lvl
        for a, b, snr in carrier:
            if snr < 14:            # threshold clicks ("popcorn") on weak signals
                k = rng.poisson((b - a) * (14 - snr) * 0.8)
                for i in rng.integers(int(a * SR), max(int(a * SR) + 1, int(b * SR) - 40), k):
                    y[i:i + 30] += rng.choice([-1, 1]) * 0.25 * np.hanning(30)
        t = np.arange(n) / SR
        for a, b, snr in carrier:   # the repeater's courtesy beep 0.4 s after each unkey (if nobody keyed yet)
            i = int((b + 0.4) * SR)
            if not any(a2 <= b + 0.6 and a2 > b for a2, _, _ in carrier) and i + 1920 < n:
                bt = np.arange(1920) / SR
                y[i:i + 1920] += 0.08 * np.sin(2 * np.pi * (880 if rng.random() < .5 else 1200) * bt) * np.hanning(1920)
        y += 0.004 * np.sin(2 * np.pi * 60 * t) + 0.002 * np.sin(2 * np.pi * 180 * t)      # hum
        y += 0.01 * np.sin(2 * np.pi * 100.0 * t)                                        # CTCSS
        return (np.tanh(y * 3) / 3).astype(np.float32)                                   # deviation limiter
    band_snr = {"ssb_hard": 0.0, "ssb_mod": 0.0}[kind]
    noise = bandpass(rng.normal(size=n).astype(np.float32), 150, 2900, soft=80)
    noise *= 0.1 / rms(noise)                          # band noise at 0 dB re a 0-dB-SNR station
    y = x + noise
    k = rng.poisson(n / SR * 1.5)                      # static crashes
    for i in rng.integers(0, max(1, n - 800), k):
        y[i:i + 800] += rng.normal(size=800).astype(np.float32) * 0.6 * np.exp(-np.arange(800) / 150)
    env = np.abs(y); g = np.empty(n, np.float32); level = 0.05
    a_att, a_rel = 1 - np.exp(-1 / (0.005 * SR)), 1 - np.exp(-1 / (1.5 * SR))
    for i0 in range(0, n, 160):                        # AGC: fast attack, slow release, 10 ms blocks
        e = float(env[i0:i0 + 160].max())
        level += min(1.0, (a_att if e > level else a_rel) * 160) * (e - level)
        level = max(level, 1e-4)
        g[i0:i0 + 160] = 0.25 / level
    return (np.clip(y * np.minimum(g, 40), -1, 1) * 0.5).astype(np.float32)


def channel(rng, kind, s, seg):
    """One transmission's audio through the radio path (speech at unit level, noise at the station's SNR)."""
    speech = seg / (rms(seg[np.abs(seg) > 0.01]) if (np.abs(seg) > 0.01).any() else 1) * 0.1
    n = len(speech)
    if kind == "fm":
        y = bandpass(speech, 250, 3100)
        if s.get("mobile"):          # multipath flutter: short deep dips
            env = np.abs(slow_noise(rng, n, 6.0)); env = np.clip(env / np.median(env), 0, 1.5)
            y = y * env.astype(np.float32)
        return y.astype(np.float32)
    # SSB: the speaker's signal at its SNR over the (unit) band noise added in finish()
    y = bandpass(speech, 300, 2700, soft=60)
    y = freq_shift(y, s["shift"])
    fad = np.abs(0.6 + 0.4 * slow_noise(rng, n, rng.uniform(0.2, 0.8)) / np.sqrt(2))
    y = y * fad.astype(np.float32) * 10 ** (s["snr"] / 20)
    return y.astype(np.float32)


class OracleGate:
    def __init__(self, on):
        self.on, self.i = on, 0

    def is_speech(self, frame):
        v = self.on[self.i] if self.i < len(self.on) else False
        self.i += 1
        return bool(v)


def cut(kind, x, carrier, spans, base_epoch):
    fl = 512
    nfr = len(x) // fl
    on = np.zeros(nfr, bool)
    if kind == "fm":
        # carrier gate: the repeater keeps the carrier up for its hang time (2 s) after each unkey
        merged = []
        for a, b in carrier:
            if merged and a <= merged[-1][1] + 2.0:
                merged[-1][1] = b
            else:
                merged.append([a, b])
        for a, b in merged:
            on[int(a * SR / fl): int((b + 0.5) * SR / fl)] = True
        # outside the carrier the data jack carries loud discriminator hiss (measured +20 dB over in-carrier noise)
        hiss = np.random.default_rng(1).normal(size=len(x)).astype(np.float32) * 0.18
        mask = np.repeat(on, fl)
        mask = np.concatenate([mask, np.zeros(len(x) - len(mask), bool)])
        x = np.where(mask, x, hiss).astype(np.float32)
        split = 800
    else:
        for a, b, _, _ in spans:          # a voice gate (oracle VAD)
            on[int(a * SR / fl): int(b * SR / fl) + 1] = True
        r = np.random.default_rng(len(x))   # and its false triggers on static (~1 per 40 s)
        for _ in range(int(len(x) / SR / 40)):
            s0 = r.uniform(0, len(x) / SR - 3)
            if not on[int(s0 * SR / fl): int((s0 + 3) * SR / fl)].any():
                on[int(s0 * SR / fl): int((s0 + r.uniform(0.5, 1.5)) * SR / fl)] = True
        split = 0
    clock = {"t": base_epoch}
    seg = Segmenter(OracleGate(on), SR, 32, 400, 900, 180, 250, 1.0, clock=lambda: clock["t"],
                    split_pause_ms=split, min_split_s=2.0)
    pieces = []
    for i in range(nfr):
        clock["t"] = base_epoch + (i + 1) * fl / SR
        for ev in seg.feed(x[i * fl:(i + 1) * fl]):
            if ev[0] == "end" and not ev[2]["kerchunk"]:
                pieces.append((ev[2]["t0"], ev[1]))
    return x, pieces


def main():
    out, kind, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    rng = random.Random(seed)
    ncs, stations, spk, tx = script(rng, kind, n)
    x, carrier, spans = build(rng, kind, spk, tx)
    base = datetime(2026, 11, 3, 19, 0, 0).timestamp() + seed * 7200 + rng.random()
    x, pieces = cut(kind, x, carrier, spans, base)
    os.makedirs(out + "/tx", exist_ok=True)
    sf.write(out + "/timeline.wav", x, SR, subtype="PCM_16")
    log, truth = [], []
    for i, (t0, a) in enumerate(pieces):
        tid = f"{seed}{i:04d}"
        sf.write(f"{out}/tx/{tid}.wav", a, SR, subtype="PCM_16")
        a0, a1 = t0 - base, t0 - base + len(a) / SR
        mine = [s for s in spans if a0 <= (s[0] + s[1]) / 2 < a1]
        straddle = sum(1 for s in spans if s[0] < a0 < s[1] - 0.05 or s[0] + 0.05 < a1 < s[1])
        log.append({"id": tid, "radio": kind, "freq_mhz": 442.1 if kind == "fm" else 3.915,
                    "start": datetime.fromtimestamp(t0).isoformat(timespec="seconds"), "duration_s": round(len(a) / SR, 2),
                    "wav": f"tx/{tid}.wav"})
        truth.append({"id": tid, "ref": " ".join(s[2] for s in mine), "calls": [], "t0": a0, "t1": a1, "straddle": straddle,
                      "speakers": sorted({tx[s[3]][0] for s in mine})})
    json.dump({"kind": kind, "seed": seed, "ncs": ncs, "stations": stations, "calls": [ncs["call"]] + [p["call"] for p in stations],
               "spans": spans, "speakers": {k: {kk: vv for kk, vv in v.items()} for k, v in spk.items()}},
              open(out + "/net.json", "w"), indent=1, default=str)
    with open(out + "/log.jsonl", "w") as f:
        for r in log:
            f.write(json.dumps(r) + "\n")
    json.dump(truth, open(out + "/truth.json", "w"), indent=1)
    dropped = [s for s in spans if not any(t["t0"] <= (s[0] + s[1]) / 2 < t["t1"] for t in truth)]
    print(f"{out}: {len(tx)} turns, {len(spans)} phrases, {len(pieces)} pieces, audio {len(x)/SR:.0f}s, "
          f"pieces {sum(len(a) for _, a in pieces)/SR:.0f}s, phrases dropped by the gate {len(dropped)}, straddling {sum(t['straddle'] for t in truth)}")


if __name__ == "__main__":
    main()
