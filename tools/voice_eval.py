"""Measure whether voice backfill (station/speaker.py backfill + station/voiceprint.py) can be trusted on a real net.
Run it before ever switching voice backfill on; it is NOT on in the station (see README "Who is talking").

There are no human labels, so the labels come from the words:
  NCS   = a pure acknowledgement with no callsign in it (net control's words; one person)
  CALL  = a transmission that states its own callsign (speaker.stated)
Reports: similarity of same-voice vs different-voice pairs; leave-one-out backfill precision (hide one label,
backfill it from the rest of the net); how often net control's acknowledgements get a check-in's call when net
control has no labelled voice; and the halves test (first half vs second half of each transmission >= 2x MIN s).

    python -m tools.voice_eval <log.jsonl or state.json> <folder of <id>_*.wav> [--model PATH] [--radio ID]

Needs onnxruntime + the model file (not in the dev venv; use a scratch venv). 2026-11-02 result on the 3.915 MHz
LSB net (161 transmissions): same-voice and different-voice similarities overlap almost completely (halves test
EER ~0.31, rank-1 0.08-0.20 for 5 WeSpeaker models) -> voice backfill stays off.
"""
import argparse, glob, json, os, sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from station.speaker import backfill, from_text            # noqa: E402
from station.voiceprint import MODEL_PATH, VoicePrint       # noqa: E402


def load_tx(path):
    p = Path(path)
    if p.suffix == ".json":
        return json.loads(p.read_text())["tx"]
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip() and "update" not in json.loads(x)]


def center(m):
    m = m - m.mean(0)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log"); ap.add_argument("wavs")
    ap.add_argument("--model", default=str(MODEL_PATH)); ap.add_argument("--radio")
    ap.add_argument("--keep", type=float, default=1.0); ap.add_argument("--half-min-s", type=float, default=3.0)
    a = ap.parse_args()
    vp = VoicePrint(a.model, keep=a.keep)
    import soundfile as sf
    emb, halves = {}, []
    for p in sorted(glob.glob(os.path.join(a.wavs, "*.wav"))):
        x, sr = sf.read(p, dtype="float32", always_2d=True)
        x = x.mean(1)
        tid = os.path.basename(p).split("_")[0]
        e = vp.embed(x)
        if e is not None:
            emb[tid] = e
        if len(x) >= 2 * a.half_min_s * sr:
            h = len(x) // 2
            halves.append((vp.embed(x[:h]), vp.embed(x[h:])))
    ids = list(emb)
    E = dict(zip(ids, center(np.stack([emb[i] for i in ids]).astype(np.float64))))   # remove the shared channel
    lab = {}
    for t in load_tx(a.log):
        if t["id"] in E and t.get("status") == "final" and (not a.radio or t.get("radio") == a.radio):
            s, how = from_text(t.get("text", ""), t.get("callsigns", []), ncs="NCS")
            if how == "ncs" and not t.get("callsigns"):
                lab[t["id"]] = "NCS"
            elif how == "stated":
                lab[t["id"]] = s
    L = list(lab)
    n_ncs = sum(v == "NCS" for v in lab.values())
    print(f"{len(E)} embedded; labelled {n_ncs} net-control acknowledgements, {len(L) - n_ncs} stated")

    def pairs(f):
        return np.array([float(E[x] @ E[y]) for i, x in enumerate(L) for y in L[i + 1:] if f(lab[x], lab[y])])
    for name, v in [("same voice (NCS-NCS)", pairs(lambda x, y: x == y == "NCS")),
                    ("different (NCS-stated)", pairs(lambda x, y: (x == "NCS") != (y == "NCS"))),
                    ("different (stated, other call)", pairs(lambda x, y: "NCS" not in (x, y) and x != y))]:
        if len(v):
            print(f"  {name:32} n={len(v):4} mean={v.mean():+.3f} p90={np.percentile(v, 90):+.3f}")
    print("leave-one-out: thr margin assigned correct precision")
    for thr in (0.3, 0.4, 0.5):
        for margin in (0.05, 0.10):
            ok = bad = 0
            for q in L:
                r = backfill([{"id": i, "emb": E[i], "call": lab[i] if i != q else None} for i in L], thr, margin).get(q)
                if r:
                    ok, bad = ok + (r["call"] == lab[q]), bad + (r["call"] != lab[q])
            print(f"  {thr:.1f} {margin:.2f} {ok + bad:4} {ok:4} {ok / max(1, ok + bad):.2f}")
    for thr in (0.3, 0.4, 0.5):
        r = backfill([{"id": i, "emb": E[i], "call": lab[i] if lab[i] != "NCS" else None} for i in L], thr, 0.05)
        print(f"  no net-control voice, thr {thr:.1f}: {sum(lab[i] == 'NCS' for i in r)} of {n_ncs} acknowledgements given a check-in's call")
    hv = [(x, y) for x, y in halves if x is not None and y is not None]
    if len(hv) > 2:
        Z = center(np.concatenate([np.stack(z) for z in zip(*hv)]).astype(np.float64))
        X, Y = Z[:len(hv)], Z[len(hv):]
        S = X @ Y.T
        same, diff = np.diag(S), S[~np.eye(len(S), dtype=bool)]
        th = np.sort(np.concatenate([same, diff]))
        eer_t = min(th, key=lambda t: abs(np.mean(same < t) - np.mean(diff >= t)))
        print(f"halves (n={len(S)}): same {same.mean():+.3f} vs different {diff.mean():+.3f}; "
              f"rank-1 {np.mean(S.argmax(1) == np.arange(len(S))):.2f}; EER ~{np.mean(same < eer_t):.2f}")


if __name__ == "__main__":
    main()
