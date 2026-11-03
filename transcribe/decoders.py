"""Whisper decoders for the accurate pass with context (transcribe/context.py). Each one is a function
`decode(audio, prompt="") -> segments` (float32 mono 16 kHz audio of any length) returning mlx_whisper's shape:
[{"start", "end", "text", "compression_ratio"?, "avg_logprob"?, "no_speech_prob"?,
  "words": [{"word", "start", "end", "probability"}]}].

  mlx             the home server (transcribe/server.py): large-v3 on Apple silicon
  openvino        a station PC's Intel GPU / NPU / CPU (openvino-genai WhisperPipeline, word timestamps).
                  It gives NO word probabilities: "probability" is None and context.py merges the two decodes by
                  how far each word is from a window cut instead (README "How the accurate pass works")
  faster-whisper  NVIDIA (CUDA) or any CPU (CTranslate2); word probabilities, like mlx

All decode the way the Mac does: English, no conditioning on the previous window's text, temperature fallback when a
window comes out repetitive, Whisper's no-speech skip off (FM hiss made it drop whole windows of check-ins).

`pause` (station PCs): called before each ~30 s window. The station's local pass blocks there while the live text
needs the machine (station/accurate_local.py), so a long block never holds the GPU for more than one window.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

SR = 16000
WINDOW_S = 30.0
TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)     # Whisper's fallback when a window comes out repetitive


def mlx(model: str):
    """mlx_whisper on the home server. Whisper's own sequential long-form decode."""
    import mlx_whisper

    def decode(x, prompt=""):
        r = mlx_whisper.transcribe(x, path_or_hf_repo=model, language="en", initial_prompt=prompt or None,
                                   condition_on_previous_text=False, temperature=TEMPERATURES, word_timestamps=True,
                                   no_speech_threshold=None)
        return r.get("segments") or []
    return decode


def faster_whisper(model: str, device: str = "cpu", compute_type: str | None = None, pause=None, threads: int = 0,
                   fallback: bool = True):
    """CTranslate2. Its transcribe() is a generator that decodes a 30 s window when the next segment is asked for, so
    `pause` between segments holds the GPU/CPU between windows."""
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device=device, compute_type=compute_type or ("float16" if device == "cuda" else "int8"),
                     cpu_threads=threads or 0)

    def decode(x, prompt=""):
        segs, _ = m.transcribe(np.asarray(x, np.float32), language="en", initial_prompt=prompt or None,
                               condition_on_previous_text=False, beam_size=1,
                               temperature=list(TEMPERATURES) if fallback else [0.0], word_timestamps=True, no_speech_threshold=None, vad_filter=False)
        out, it = [], iter(segs)
        while True:
            if pause:
                pause()
            try:
                s = next(it)
            except StopIteration:
                return out
            out.append({"start": s.start, "end": s.end, "text": s.text, "avg_logprob": s.avg_logprob,
                        "compression_ratio": s.compression_ratio, "no_speech_prob": s.no_speech_prob,
                        "words": [{"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                                  for w in (s.words or [])]})
    return decode


def ov_segments(chunks, words, dur: float) -> list[dict]:
    """openvino-genai's result (chunks = segments with start_ts/end_ts/text; words with word/start_ts/end_ts) ->
    segments with their words. A chunk with no end (end_ts < 0: the window ran out mid-segment) ends at `dur`."""
    segs = []
    for c in chunks or []:
        a = max(0.0, float(c.start_ts))
        b = float(c.end_ts) if float(c.end_ts) >= 0 else dur
        segs.append({"start": a, "end": max(a, b), "text": c.text, "words": []})
    if not segs:
        segs = [{"start": 0.0, "end": dur, "text": "", "words": []}]
    for w in words or []:
        t0, t1 = float(w.start_ts), float(w.end_ts)
        mid = (t0 + t1) / 2
        seg = next((s for s in segs if s["start"] <= mid < s["end"]), None) or \
            min(segs, key=lambda s: min(abs(s["start"] - mid), abs(s["end"] - mid)))
        seg["words"].append({"word": w.word, "start": t0, "end": t1, "probability": None})
    for s in segs:
        if not s["text"].strip() and s["words"]:
            s["text"] = "".join(w["word"] for w in s["words"])
    return segs


def window_failed(segs) -> bool:
    """Whisper's reason to try a window again at a higher temperature: it came out repetitive (a loop)."""
    from .context import compression_ratio, looped
    for s in segs:
        t = s.get("text") or ""
        if compression_ratio(t) > 2.4 or looped(t.split()):
            return True
    return False


def with_fallback(decode_window, temperatures=TEMPERATURES):
    """decode_window(x, prompt, temperature) -> segments; retried at the next temperature while it loops."""
    def run(x, prompt=""):
        best = None
        for t in temperatures:
            segs = decode_window(x, prompt, t)
            if not window_failed(segs):
                return segs
            best = best or segs
        return best or []
    return run


def sequential(x, decode_window, pause=None, window_s=WINDOW_S, min_advance_s=5.0) -> list[dict]:
    """Whisper's long-form walk, one window at a time: decode 30 s from `seek`; when the window was full and its last
    segment runs to the window's end (probably cut mid-sentence), drop that segment and start the next window where
    it began; otherwise go on from the window's end. Times come back on x's clock."""
    total = len(x) / SR
    seek, out = 0.0, []
    while seek < total - 0.05:
        if pause:
            pause()
        piece = x[int(seek * SR):int((seek + window_s) * SR)]
        dur = len(piece) / SR
        segs = decode_window(piece) or []
        adv = dur
        if dur >= window_s - 0.05 and len(segs) > 1:
            last = segs[-1]
            if last["end"] >= dur - 1.0 and last["start"] >= min_advance_s:
                segs, adv = segs[:-1], last["start"]
        for s in segs:
            out.append({**s, "start": s["start"] + seek, "end": s["end"] + seek,
                        "words": [{**w, "start": w["start"] + seek, "end": w["end"] + seek} for w in s["words"]]})
        seek += adv
    return out


def openvino(model_dir, device: str, pause=None, cache_dir: str | None = None, threads: int = 0, fallback: bool = True):
    """openvino-genai WhisperPipeline with word timestamps. Never create an openvino.Core in this process (a Core
    beside a GPU WhisperPipeline hangs the process at exit on Windows; CLAUDE.md). On the GPU the queue priority is
    LOW so the live engine's work goes first."""
    import openvino_genai as ov
    props = {}
    if cache_dir:
        props["CACHE_DIR"] = str(cache_dir)
    if device.startswith("GPU"):
        props["GPU_QUEUE_PRIORITY"] = "LOW"
    if device == "CPU" and threads:
        props["INFERENCE_NUM_THREADS"] = int(threads)
    pipe = ov.WhisperPipeline(str(model_dir), device, word_timestamps=True, **props)
    english_only = ".en-" in Path(model_dir).name

    def window(x, prompt="", temperature=0.0):
        cfg = pipe.get_generation_config()
        cfg.return_timestamps = True
        cfg.word_timestamps = True
        if not english_only:
            cfg.language = "<|en|>"; cfg.task = "transcribe"
        if prompt:
            cfg.initial_prompt = prompt
        if temperature > 0:
            cfg.do_sample = True; cfg.temperature = temperature
        r = pipe.generate(np.asarray(x, np.float32), cfg)
        return ov_segments(r.chunks, r.words, len(x) / SR)

    win = with_fallback(window, TEMPERATURES if fallback else (0.0,))

    def decode(x, prompt=""):
        return sequential(np.asarray(x, np.float32), lambda piece: win(piece, prompt), pause)
    return decode


def fake(pause=None):
    """Tests: 'hears' one word per burst of sound (w0, w1, ...), probability None like OpenVINO."""
    def window(x, prompt="", temperature=0.0):
        env = np.convolve(np.abs(x), np.ones(160) / 160, "same") > 0.05
        words, i = [], 0
        while i < len(env):
            if env[i]:
                j = i
                while j < len(env) and env[j]:
                    j += 1
                words.append({"word": f" w{len(words)}", "start": i / SR, "end": j / SR, "probability": None})
                i = j
            else:
                i += 1
        return [{"start": 0.0, "end": len(x) / SR, "text": "".join(w["word"] for w in words), "words": words}]

    def decode(x, prompt=""):
        return sequential(np.asarray(x, np.float32), lambda piece: window(piece, prompt), pause)
    return decode


def make(spec: dict, models_dir: Path | None = None, pause=None, cache_dir=None, fallback: bool = True):
    """spec = engine.yaml's `accurate` entry: {"engine": "openvino" | "faster-whisper" | "mlx", "model", "device"}."""
    eng, model, device = spec["engine"], spec["model"], spec.get("device", "CPU")
    local = Path(models_dir) / model if models_dir else None
    if eng == "openvino":
        return openvino(local or model, device, pause=pause, cache_dir=cache_dir, threads=spec.get("threads", 0),
                        fallback=fallback)
    if eng == "faster-whisper":
        return faster_whisper(str(local) if local and local.exists() else model, device.lower(), pause=pause,
                              threads=spec.get("threads", 0), fallback=fallback)
    if eng == "mlx":
        return mlx(model)
    if eng == "fake":
        return fake(pause)
    raise ValueError(f"unknown accurate engine {eng}")
