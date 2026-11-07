"""Who? suggestions by voice: "sounds like K7ABC (said at 19:42)" for a transmission that names nobody.

Only a SUGGESTION in the operator's Who? dialog, never a name in the log or the ICS-309 until the operator saves it.
Measured 2026-11-07 on five days of FM nets (1,880 transmissions, two receivers, WeSpeaker ResNet34):
  - across days a station's voice is no better than chance (its radio and place change): only the SAME net counts;
  - within a net, the closest voice that clearly beats the next one (score >= 0.40, ahead by >= 0.20) named the
    speaker right in 15 of 17 cases (88 %, 95 % range 66-97 %) for about half of the transmissions whose voice
    was known in that net; a higher score alone did not help (0.60: 3 of 5).
  - labels: only transmissions whose speaker stated their own call or was set by the operator (Who?).
Each Who? save records what was suggested, so the operator's answers measure it again later.

Voiceprints: station/voiceprint.py (onnxruntime, CPU, 2 threads), computed in the background as transmissions
finish (2 s or longer), cached in data/voice/<id>.npy. No model file -> no suggestions, nothing else changes.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

MIN_S = 2.0             # shorter transmissions carry too little voice (measured: 2 s and up)
SCORE = 0.40            # the closest voice must be at least this alike ...
GAP = 0.20              # ... and this far ahead of the next station's voice
WINDOW_S = 2 * 3600     # no scheduled net: the same radio and frequency within 2 hours
MAX_GROUP = 300         # the transmissions nearest in time that are compared


def _t(tx) -> float:
    try:
        return datetime.fromisoformat(tx["start"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def same_net(a: dict, b: dict) -> bool:
    if a.get("radio") != b.get("radio"):
        return False
    na, nb = (a.get("net") or {}).get("key"), (b.get("net") or {}).get("key")
    if na or nb:
        return na == nb
    fa, fb = a.get("freq_mhz"), b.get("freq_mhz")
    return (fa is None or fb is None or abs(fa - fb) < 1e-4) and abs(_t(a) - _t(b)) <= WINDOW_S


def suggest(target: dict, items: list[dict], vec, label) -> dict:
    """target: the transmission; items: the station's transmissions; vec(tx) -> unit vector or None;
    label(tx) -> the callsign whose speaker is KNOWN (stated / operator) or None.
    -> {"call", "score", "gap", "at", "id"} or {"none": why}."""
    v0 = vec(target)
    if v0 is None:
        return {"none": "too short to compare voices" if (target.get("duration_s") or 0) < MIN_S else "no voiceprint"}
    group = [t for t in items if t is not target and t.get("id") != target.get("id") and same_net(target, t)]
    group.sort(key=lambda t: abs(_t(t) - _t(target)))
    group = group[:MAX_GROUP]
    vecs = [(t, vec(t)) for t in group]
    vecs = [(t, v) for t, v in vecs if v is not None]
    known = [(t, v, label(t)) for t, v in vecs if label(t)]
    if not known:
        return {"none": "nobody has stated their call in this net yet"}
    # what the whole net shares (receiver, repeater, channel) is not anyone's voice
    mean = np.mean([v for _, v in vecs] + [v0], axis=0)
    norm = lambda x: (x - mean) / (np.linalg.norm(x - mean) or 1.0)
    q = norm(v0)
    best: dict[str, tuple[float, dict]] = {}
    for t, v, call in known:
        s = float(q @ norm(v))
        if call not in best or s > best[call][0]:
            best[call] = (s, t)
    ranked = sorted(((s, c, t) for c, (s, t) in best.items()), reverse=True)
    s1, call, t1 = ranked[0]
    s2 = ranked[1][0] if len(ranked) > 1 else -1.0
    if s1 < SCORE or s1 - s2 < GAP:
        return {"none": "no voice in this net is a clear match", "closest": call, "score": round(s1, 2),
                "gap": round(s1 - s2, 2)}
    return {"call": call, "score": round(s1, 2), "gap": round(s1 - s2, 2), "at": t1.get("start"), "id": t1.get("id")}


class VoiceHints:
    """Background voiceprints for finished transmissions + Who? suggestions."""

    def __init__(self, data_dir: Path, model_path: Path | None = None, maker=None, enabled: bool = True):
        from . import voiceprint
        self.data = Path(data_dir)
        self.dir = self.data / "voice"
        self.model_path = Path(model_path or voiceprint.MODEL_PATH)
        self.maker = maker or (lambda: voiceprint.VoicePrint(self.model_path, threads=2))
        self.on = enabled and (maker is not None or voiceprint.VoicePrint.available(self.model_path))
        self._vp = None
        self._lock = threading.Lock()
        self._mem: dict[str, np.ndarray | None] = {}
        self._q: queue.Queue = queue.Queue()
        if self.on:
            threading.Thread(target=self._worker, daemon=True, name="voiceprints").start()

    def queue(self, tx: dict):
        if self.on and (tx.get("duration_s") or 0) >= MIN_S and tx.get("wav"):
            self._q.put(dict(tx))

    def _worker(self):
        while True:
            tx = self._q.get()
            try:
                self.vec(tx)
            except Exception:  # noqa: BLE001 — a voiceprint is a nicety; never stop the station
                pass

    def vec(self, tx: dict):
        tid = str(tx.get("id"))
        if tid in self._mem:
            return self._mem[tid]
        if not self.on or (tx.get("duration_s") or 0) < MIN_S or not tx.get("wav"):
            return None
        f = self.dir / f"{tid}.npy"
        v = None
        try:
            if f.exists():
                v = np.load(f)
            else:
                import soundfile as sf
                x, _ = sf.read(str(self.data / tx["wav"].replace("\\", "/")), dtype="float32", always_2d=True)
                with self._lock:
                    if self._vp is None:
                        self._vp = self.maker()
                    e = self._vp.embed(x.mean(axis=1))
                if e is not None:
                    v = np.asarray(e, np.float32)
                    self.dir.mkdir(parents=True, exist_ok=True)
                    np.save(f, v)
        except Exception:  # noqa: BLE001 — unreadable or missing recording: no voiceprint
            v = None
        self._mem[tid] = v
        return v

    def suggest(self, target: dict, items: list[dict]) -> dict:
        if not self.on:
            return {"none": "voice suggestions are off (no voice model on this PC)"}
        from .speaker import best

        def label(t):
            call, how = best(t)
            return call if how in ("stated", "operator") else None
        return suggest(target, items, self.vec, label)
