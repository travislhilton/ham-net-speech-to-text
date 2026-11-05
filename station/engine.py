"""Whisper engines for the station, as chosen by tools/pick_engine.py (config/engine.yaml).

Rules learned on the test PC (2026-11-02):
- never create an `openvino.Core` in this process: Core + a GPU WhisperPipeline hangs the process at exit;
- English-only models (*.en) must not be given a language;
- each pipeline is created and used by ONE thread (the worker that owns it).
"""
from pathlib import Path

import numpy as np
import yaml

from .textclean import collapse_repeats, max_tokens

ROOT = Path(__file__).resolve().parent.parent


class Engine:
    name = "none"
    warm = True            # run once at startup (GPU kernel compile); fakes opt out

    def transcribe(self, audio: np.ndarray, prompt: str | None = None) -> dict:
        raise NotImplementedError


class OpenVINOEngine(Engine):
    def __init__(self, model: str, device: str, models_dir: Path, prompt: str = ""):
        import openvino_genai as ov
        self.name = f"{model}@{device}"
        self.english_only = ".en-" in model
        # Compiled-model cache (the accurate pass shares it): a GPU or NPU compile takes minutes on some PCs (a laptop station,
        # 2026-11-04: turbo on the GPU 36 s -> 16 s, small.en on the NPU 555 s -> 11 s) and would repeat at every start.
        cache = Path(models_dir).parent / "data" / "ov-cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            props = {"CACHE_DIR": str(cache)}
        except OSError:
            props = {}
        self.pipe = ov.WhisperPipeline(str(models_dir / model), device, **props)
        self.prompt = prompt

    def transcribe(self, audio, prompt=None):
        cfg = self.pipe.get_generation_config()
        cfg.max_new_tokens = max_tokens(len(audio) / 16000)
        if not self.english_only:
            cfg.language = "<|en|>"; cfg.task = "transcribe"
        p = self.prompt if prompt is None else prompt
        if p:
            cfg.initial_prompt = p
        text, looped = collapse_repeats(str(self.pipe.generate(audio.astype(np.float32), cfg)).strip())
        return {"text": text, "looped": looped, "model": self.name}


class FasterWhisperEngine(Engine):
    def __init__(self, model: str, device: str, prompt: str = ""):
        from faster_whisper import WhisperModel
        self.name = f"{model}@{device}"
        self.m = WhisperModel(model, device=device, compute_type="float16" if device == "cuda" else "int8")
        self.prompt = prompt

    def transcribe(self, audio, prompt=None):
        p = self.prompt if prompt is None else prompt
        segs, _ = self.m.transcribe(audio.astype(np.float32), language="en", beam_size=1,
                                    initial_prompt=p or None, condition_on_previous_text=False)
        text, looped = collapse_repeats(" ".join(s.text.strip() for s in segs))
        return {"text": text, "looped": looped, "model": self.name}


class FakeEngine(Engine):
    """Tests and --engine fake: returns a scripted text (or a fixed one)."""
    warm = False
    def __init__(self, texts=None, name="fake"):
        self.texts, self.name, self.calls = list(texts or []), name, 0

    def transcribe(self, audio, prompt=None):
        self.calls += 1; self.last_prompt = prompt
        t = self.texts.pop(0) if self.texts else "This is W9XYZ testing."
        return {"text": t, "looped": False, "model": self.name}


def load_choice(path: Path = ROOT / "config" / "engine.yaml") -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def make(spec: dict | None, models_dir: Path = ROOT / "models", prompt: str | None = None) -> Engine | None:
    """spec = engine.yaml's `final` or `partial` entry."""
    if not spec:
        return None
    # The long vocabulary prompt (~900 chars) sent OpenVINO turbo into repetition loops on real traffic
    # (test PC, 2026-11-02: "TESTING TESTING …"); the station passes a short per-transmission prompt.
    p = "" if prompt is None else prompt
    if spec["engine"] == "openvino":
        return OpenVINOEngine(spec["model"], spec["device"], models_dir, p)
    if spec["engine"] == "faster-whisper":
        return FasterWhisperEngine(spec["model"], spec["device"], p)
    if spec["engine"] == "fake":
        return FakeEngine()
    raise ValueError(f"unknown engine {spec['engine']}")
