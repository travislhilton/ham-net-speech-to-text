"""Score what run.py saved.

Synthetic nets (truth known): WER over the net (every transmission's text in log order against the truth in the
same order), mean per-transmission WER, callsign mentions per transmission (recall, precision), the roster (unique
calls found / false). Real nets (no truth): FCC-valid unique calls, the share of call-shaped tokens that are
FCC-valid (a precision proxy), "Thank you."-style noise texts, transmissions set aside.

    python -m tools.accbench.score NET... --methods=live,clip,prov,final [--fallback]
--fallback: where a method set a transmission aside (empty/echo/loop/noise/low-confidence), score the live text
instead: what the station shows.
"""
import json, os, re, sqlite3, sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from hamkit.callsigns import _ANY, normalize
from hamkit.fcc import DEFAULT_DB
from station.textclean import collapse_repeats, template_loop
from tools.accbench.run import hyp_dir

_con = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
_v = {}
NUM = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
       "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
CONTR = {"you're": "you are", "we'll": "we will", "i'm": "i am", "that's": "that is", "it's": "it is",
         "there's": "there is", "we're": "we are", "don't": "do not", "i've": "i have", "let's": "let us"}
BAD = {"empty", "echo", "loop", "noise_phrase", "low_conf"}


def valid(c):
    if c not in _v:
        _v[c] = _con.execute("select 1 from hd where call=? limit 1", (c,)).fetchone() is not None
    return _v[c]


def norm(text):
    """Words for WER: callsigns normalised ("Kilo Seven Alpha..." -> k7a..), numbers as digits, contractions open."""
    t, calls = normalize(text or "", validate=valid)
    t = re.sub(r"\bseventy[- ]three\b", "73", t, flags=re.I).replace("-", " ")
    toks = []
    for w in re.findall(r"[A-Za-z0-9']+", t):
        lw = w.lower()
        if lw in CONTR:
            toks += CONTR[lw].split()
        else:
            toks.append(lw if (w.isupper() and any(ch.isdigit() for ch in w)) else NUM.get(lw, lw))
    return toks, calls


def wer_counts(ref, hyp):
    n, m = len(ref), len(hyp)
    d = np.arange(m + 1)
    for i in range(1, n + 1):
        prev, d = d, np.empty(m + 1, int)
        d[0] = i
        for j in range(1, m + 1):
            d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (ref[i - 1] != hyp[j - 1]))
    return int(d[m]), n


def texts(net, method, fallback):
    h = json.load(open(os.path.join(hyp_dir(net), method + ".json")))
    lf = os.path.join(hyp_dir(net), "live.json")
    live = json.load(open(lf))["results"] if fallback and os.path.exists(lf) else {}
    out, aside = {}, 0
    for k, v in h["results"].items():
        t = v["text"]
        # the station's own guards (station/core.py apply_accurate): a flagged clip, an empty text, or a loop
        # (the station keeps the live text for those whatever the method; 2026-11-06 the bench left loops in)
        off = bool(set(v.get("flags") or []) & BAD) or not t.strip() or collapse_repeats(t)[1] or template_loop(t)
        if off:
            aside += 1
            t = live.get(k, {}).get("text", "") if fallback else ""
        out[k] = t
    return out, aside, h


def score_syn(net, method, fallback):
    log = [json.loads(line) for line in open(os.path.join(net, "log.jsonl"))]
    tr = {t["id"]: t for t in json.load(open(os.path.join(net, "truth.json")))}
    hyp, aside, h = texts(net, method, fallback)
    per, cm, allref, allhyp, rr, hr = [], Counter(), [], [], set(), set()
    for r in log:
        rt, _ = norm(tr[r["id"]]["ref"])
        ht, hc = norm(hyp.get(r["id"], ""))
        allref += rt; allhyp += ht
        e, n = wer_counts(rt, ht)
        if n:
            per.append(e / n)
        rcs, hcs = Counter(re.findall(r"\b[A-Z]{1,2}\d[A-Z]{1,3}\b", tr[r["id"]]["ref"])), Counter(hc)
        cm["ref"] += sum(rcs.values()); cm["hyp"] += sum(hcs.values()); cm["hit"] += sum((rcs & hcs).values())
        rr |= set(rcs); hr |= set(hcs)
    e, n = wer_counts(allref, allhyp)
    return {"e": e, "n": n, "tx": float(np.mean(per)) if per else 0.0, **cm, "aside": aside,
            "found": len(rr & hr), "roster": len(rr), "false": len(hr - rr), "el": h.get("elapsed_s") or 0,
            "au": h.get("audio_s") or 0}


def score_real(net, method, fallback):
    hyp, aside, h = texts(net, method, fallback)
    shaped = ok = noise = 0
    uniq = set()
    for t in hyp.values():
        for c in re.findall(r"\b[A-Z0-9]{3,8}\b", normalize(t, validate=valid)[0]):
            if _ANY.match(c):
                shaped += 1
                if valid(c):
                    ok += 1; uniq.add(c)
        noise += re.sub(r"[^a-z ]", "", t.lower()).strip() in ("thank you", "so", "okay", "you", "bye", "thanks")
    return (f"valid calls {len(uniq):3d}  valid share {100 * ok / max(1, shaped):5.1f}%  noise texts {noise:3d}  "
            f"aside {aside}/{len(hyp)}  {h.get('elapsed_s')} s for {h.get('audio_s')} s")


def main():
    nets = [a for a in sys.argv[1:] if not a.startswith("--")]
    methods = next(a for a in sys.argv if a.startswith("--methods=")).split("=", 1)[1].split(",")
    fb = "--fallback" in sys.argv
    for m in methods:
        syn = []
        for net in nets:
            if not os.path.exists(os.path.join(hyp_dir(net), m + ".json")):
                continue
            if ":" in net and not os.path.isdir(net):
                print(f"{m:8s} {net}: {score_real(net, m, fb)}")
            else:
                syn.append((net, score_syn(net, m, fb)))
        if syn:
            S = {k: sum(s[k] for _, s in syn) for k in ("e", "n", "ref", "hyp", "hit", "found", "roster", "false", "aside", "el", "au")}
            print(f"{m:8s} WER {100 * S['e'] / S['n']:5.1f}%  per-tx {100 * np.mean([s['tx'] for _, s in syn]):5.1f}%  "
                  f"calls recall {100 * S['hit'] / max(1, S['ref']):5.1f}% precision {100 * S['hit'] / max(1, S['hyp']):5.1f}%  "
                  f"roster {S['found']}/{S['roster']} false {S['false']}  aside {S['aside']}  "
                  f"{S['au'] / max(S['el'], 1e-9):.1f}x real time  "
                  + "  ".join(f"{os.path.basename(n.rstrip('/'))}:{100 * s['e'] / max(1, s['n']):.1f}" for n, s in syn))


if __name__ == "__main__":
    main()
