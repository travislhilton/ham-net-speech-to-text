"""Run accurate-pass methods over a net and save each transmission's text (NETDIR/hyp/METHOD.json); score.py scores.

    python -m tools.accbench.run METHOD NETDIR...          a synthetic net from gen.py (log.jsonl + WAVs + truth)
    python -m tools.accbench.run METHOD STATION_DATA:FREQ  a real net: the station's data dir (logs/ + recordings/)
                                                           and a frequency in MHz, e.g. ~/hamnet-copy:442.1

METHODs (all on the home server, mlx_whisper):
  live    large-v3-turbo per transmission with the station's short prompt (approximates the W9XYZ-5 live text)
  clip    large-v3 per transmission, no prompt, T=0 (the accurate pass before 2026-11-03)
  prov    the provisional stage: each transmission decoded with the <= 26 s before it (transcribe/context.py)
  final   the final stage: 240 s blocks of the net with 30 s of context each side, long + windows, merged
  long    Whisper's own long-form decode over the whole stitched net (one of final's two decodes)
"""
import json, os, sys, time
from datetime import datetime

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from transcribe import context as cx

LV3 = "mlx-community/whisper-large-v3-mlx"
TURBO = "mlx-community/whisper-large-v3-turbo"
FB = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def highpass(x, hz):
    if not hz or len(x) < 64:
        return x
    f = np.fft.rfftfreq(len(x), 1 / 16000)
    g = 0.5 - 0.5 * np.cos(np.pi * np.clip((f - (hz - 100)) / 100, 0, 1))
    return np.fft.irfft(np.fft.rfft(x.astype(np.float64)) * g, len(x)).astype(np.float32)


def hyp_dir(net):
    if ":" in net:
        root, freq = net.rsplit(":", 1)
        return os.path.join(os.path.expanduser(root), "accbench", freq)
    return os.path.join(net, "hyp")


def load_net(net):
    """-> rows [{id, t (epoch s), duration_s, radio, freq_mhz, audio}] in log order."""
    if ":" in net:
        root, freq = net.rsplit(":", 1)
        root = os.path.expanduser(root)
        rows = []
        for name in sorted(os.listdir(os.path.join(root, "logs"))):
            rows += [json.loads(line) for line in open(os.path.join(root, "logs", name))]
        rows = [r for r in rows if "update" not in r and r.get("wav") and abs((r.get("freq_mhz") or 0) - float(freq)) < 1e-4]
        for r in rows:
            r["path"] = os.path.join(root, "recordings", r["wav"].replace("\\", "/").split("recordings/")[-1])
    else:
        rows = [json.loads(line) for line in open(os.path.join(net, "log.jsonl"))]
        for r in rows:
            r["path"] = os.path.join(net, r["wav"])
    hp = 300 if rows and rows[0].get("radio") in ("fm", "ftm150") else 0
    for r in rows:
        x, _ = sf.read(r["path"], dtype="float32")
        r["audio"] = highpass(x, hp)
        r["t"] = datetime.fromisoformat(r["start"]).timestamp()
    return rows


def whisper(model, temperature):
    import mlx_whisper

    def dec(x, prompt=""):
        r = mlx_whisper.transcribe(x, path_or_hf_repo=model, language="en", initial_prompt=prompt or None,
                                   condition_on_previous_text=False, temperature=temperature, word_timestamps=True,
                                   no_speech_threshold=None)        # as transcribe/server.py
        return r["segments"]
    return dec


def prov(rows, dec, ctx_s=26.0):
    out = {}
    for i, r in enumerate(rows):
        ctx, s = [], 0.0
        for q in reversed(rows[:i]):
            if s + q["duration_s"] > ctx_s - min(r["duration_s"], ctx_s):
                break
            ctx.insert(0, cx.Clip(q["id"], q["t"], q["audio"], target=False))
            s += q["duration_s"]
        out[r["id"]] = cx.transcribe_clips(ctx + [cx.Clip(r["id"], r["t"], r["audio"])], dec, mode="long")[r["id"]]
    return out


def final(rows, dec, block=240.0, margin=30.0):
    """As station/accurate.py sends it (blocks by transmission duration, context each side)."""
    out, i = {}, 0
    while i < len(rows):
        j, s = i, 0.0
        while j < len(rows) and (j == i or s + rows[j]["duration_s"] <= block):
            s += rows[j]["duration_s"]; j += 1
        k, b = i, 0.0
        while k > 0 and b + rows[k - 1]["duration_s"] <= margin:
            k -= 1; b += rows[k]["duration_s"]
        m, a = j, 0.0
        while m < len(rows) and a + rows[m]["duration_s"] <= margin:
            a += rows[m]["duration_s"]; m += 1
        clips = [cx.Clip(r["id"], r["t"], r["audio"], target=i <= n < j) for n, r in enumerate(rows[k:m], start=k)]
        out.update(cx.transcribe_clips(clips, dec, mode="both"))
        i = j
    return out


def run(method, rows):
    from station.core import phonetic
    out = {}
    if method in ("live", "clip"):
        import mlx_whisper
        for r in rows:
            p = (f"Ham radio net on {r.get('freq_mhz') or 0:.3f} MHz. This is W9XYZ, {phonetic('W9XYZ')}. Over."
                 if method == "live" else None)
            res = mlx_whisper.transcribe(r["audio"], path_or_hf_repo=TURBO if method == "live" else LV3, language="en",
                                         initial_prompt=p, condition_on_previous_text=False, temperature=0.0)
            out[r["id"]] = {"text": res["text"].strip()}
    elif method == "prov":
        out = prov(rows, whisper(LV3, FB))
    elif method == "final":
        out = final(rows, whisper(LV3, FB))
    elif method == "long":
        out = cx.transcribe_clips([cx.Clip(r["id"], r["t"], r["audio"]) for r in rows], whisper(LV3, FB), mode="long")
    else:
        raise SystemExit("unknown method " + method)
    return out


def main():
    method = sys.argv[1]
    for net in sys.argv[2:]:
        rows = load_net(net)
        t0 = time.time()
        res = run(method, rows)
        el = time.time() - t0
        au = sum(len(r["audio"]) for r in rows) / 16000
        os.makedirs(hyp_dir(net), exist_ok=True)
        json.dump({"results": {k: {kk: v[kk] for kk in ("text", "conf", "flags") if kk in v} for k, v in res.items()},
                   "elapsed_s": round(el, 1), "audio_s": round(au, 1)}, open(os.path.join(hyp_dir(net), method + ".json"), "w"))
        print(f"{net} {method}: {el:.1f} s for {au:.1f} s of audio ({au / el:.1f}x real time)", flush=True)


if __name__ == "__main__":
    main()
