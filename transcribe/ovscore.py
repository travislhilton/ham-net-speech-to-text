"""Word probabilities for OpenVINO Whisper: openvino-genai's WhisperPipeline returns words and times but no
probabilities (scores are always 1.0, `logprobs` changes nothing; genai 2026.4), and the accurate pass's merge of
its two decodes is decided by word probability (transcribe/context.py merge). Without them every disagreement fell
to a fixed side: on the synthetic nets the home server's own decodes lost ~4 points of word errors when their
probabilities were hidden (54.7 -> 59.0 %, 2026-11-06).

So: after each window is decoded, feed its text back through the same model once (teacher forcing: the encoder on
the window, one decoder pass over the whole token sequence, no-timestamps mode) and read each token's probability;
a word's probability = the mean of its tokens' (as openai-whisper / mlx-whisper compute it). One encoder run and
one decoder step per ~30 s window, on the same device.

The model folder is the genai one (openvino_encoder_model.xml, stateful openvino_decoder_model.xml with input_ids /
encoder_hidden_states / beam_idx, generation_config.json). Log-mel features: faster-whisper's numpy
FeatureExtractor (the installer puts faster-whisper in every station venv).

This creates an openvino.Core: run it only in a process that is ended with TerminateProcess (the accurate-pass child
and the bench are; CLAUDE.md: a Core beside a GPU WhisperPipeline hangs a process at exit).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SR = 16000
N_SAMPLES = 30 * SR
MAX_TOKENS = 440


class Scorer:
    def __init__(self, model_dir, device: str = "GPU", props: dict | None = None):
        import openvino as ov
        from faster_whisper.feature_extractor import FeatureExtractor
        d = Path(model_dir)
        g = json.loads((d / "generation_config.json").read_text(encoding="utf-8"))
        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        self.fe = FeatureExtractor(feature_size=int(cfg.get("num_mel_bins", 80)))
        multi = bool(g.get("is_multilingual", True)) and "lang_to_id" in g
        self.prefix = [int(g["decoder_start_token_id"])]
        if multi:
            self.prefix += [int(g["lang_to_id"]["<|en|>"]), int(g["task_to_id"]["transcribe"])]
        self.prefix.append(int(g["no_timestamps_token_id"]))
        core = ov.Core()
        p = dict(props or {})
        self.enc = core.compile_model(str(d / "openvino_encoder_model.xml"), device, p)
        self.dec = core.compile_model(str(d / "openvino_decoder_model.xml"), device, p)
        self.req = self.dec.create_infer_request()
        self.enc_in = self.enc.inputs[0].get_any_name()

    def features(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float32)[:N_SAMPLES]
        x = np.pad(x, (0, N_SAMPLES - len(x)))
        mel = self.fe(x, padding=0)[:, :3000]
        return mel[None].astype(np.float32)

    def token_probs(self, x: np.ndarray, tokens: list[int]) -> np.ndarray:
        """Probability of each token in `tokens` given the window audio `x` (teacher forcing)."""
        if not tokens:
            return np.zeros(0)
        tokens = tokens[:MAX_TOKENS]
        enc = self.enc({self.enc_in: self.features(x)})[self.enc.outputs[0]]
        ids = np.array([self.prefix + tokens], np.int64)
        self.req.reset_state()
        self.req.infer({"input_ids": ids, "encoder_hidden_states": enc, "beam_idx": np.array([0], np.int32)})
        logits = self.req.get_tensor("logits").data[0].astype(np.float64)       # [L, V]
        n = len(self.prefix)
        lg = logits[n - 1:n - 1 + len(tokens)]
        lg = lg - lg.max(axis=1, keepdims=True)
        logp = lg - np.log(np.exp(lg).sum(axis=1, keepdims=True))
        return np.exp(logp[np.arange(len(tokens)), tokens])

    def word_probs(self, x: np.ndarray, words) -> list[float | None]:
        """genai words (each with token_ids) -> one probability per word (mean of its tokens'); None past the cap."""
        toks, owner = [], []
        for i, w in enumerate(words):
            for t in list(getattr(w, "token_ids", []) or []):
                toks.append(int(t)); owner.append(i)
        probs = self.token_probs(x, toks)
        out: list[list[float]] = [[] for _ in words]
        for p, i in zip(probs, owner):
            out[i].append(float(p))
        return [float(np.mean(v)) if v else None for v in out]
