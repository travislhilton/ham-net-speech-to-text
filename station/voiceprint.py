"""Voice prints: a speaker embedding per transmission, so a transmission that names no callsign can borrow the
callsign of the closest voice that did name one in the same net (station/speaker.py decides; this file only
measures voices).

Model: WeSpeaker ResNet34-LM trained on VoxCeleb (CC BY 4.0,
https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM), the 26 MB ONNX export, run with onnxruntime on
the CPU. onnxruntime is already in the station's environment (faster-whisper depends on it) — deliberately NOT
OpenVINO: an openvino.Core in the process that runs the GPU WhisperPipeline hangs the station at exit (CLAUDE.md).
Features are Kaldi 80-bin log-mel filterbanks computed here in numpy (no torchaudio, no scipy), matching
torchaudio.compliance.kaldi.fbank.

The model file lives at models/voiceprint/voxceleb_resnet34_LM.onnx; absent = voice backfill off, everything else
unchanged.
"""
from pathlib import Path

import numpy as np

SR = 16000
MODEL_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "voiceprint" / "voxceleb_resnet34_LM.onnx"
MIN_S = 1.0                     # shorter than this is too little voice to compare


def _mel_banks(n_mels=80, n_fft=512, sr=SR, low=20.0, high=None) -> np.ndarray:
    high = high or sr / 2
    mel = lambda f: 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)
    lo, hi = mel(low), mel(high)
    step = (hi - lo) / (n_mels + 1)
    bins = mel(np.arange(n_fft // 2) * sr / n_fft)                         # Kaldi leaves out the Nyquist bin
    left = lo + np.arange(n_mels)[:, None] * step
    center, right = left + step, left + 2 * step
    up, down = (bins - left) / (center - left), (right - bins) / (right - center)
    w = np.maximum(0.0, np.minimum(up, down))
    return np.pad(w, ((0, 0), (0, 1)))                                     # (80, 257)


_BANKS = _mel_banks()


def fbank(audio: np.ndarray, sr: int = SR, keep: float = 1.0) -> np.ndarray:
    """Kaldi fbank (25 ms Hamming frames every 10 ms, DC removed, pre-emphasis 0.97, 512-point power spectrum,
    80 mel bins from 20 Hz, log), mean-normalised over time. `audio` is float in [-1, 1] at 16 kHz.
    keep < 1: only the loudest `keep` share of frames in the voice band (300-3000 Hz), so the hiss between words
    on a radio channel does not count as the speaker's voice."""
    x = np.asarray(audio, dtype=np.float64).reshape(-1) * 32768.0
    flen, fshift = int(sr * 0.025), int(sr * 0.010)
    if len(x) < flen:
        return np.zeros((0, 80), np.float32)
    n = 1 + (len(x) - flen) // fshift
    fr = x[np.arange(flen)[None, :] + fshift * np.arange(n)[:, None]]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] - 0.97 * fr[:, :1], fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    fr = fr * (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(flen) / (flen - 1)))[None, :]
    spec = np.abs(np.fft.rfft(fr, n=512, axis=1)) ** 2
    feats = np.log(np.maximum(spec @ _BANKS.T, np.finfo(np.float32).eps))
    if keep < 1.0 and len(feats) > 50:
        lo, hi = int(300 * 512 / sr), int(3000 * 512 / sr)
        e = np.log(spec[:, lo:hi].sum(axis=1) + 1e-9)
        feats = feats[e >= np.quantile(e, 1.0 - keep)]
    return (feats - feats.mean(axis=0, keepdims=True)).astype(np.float32)


class VoicePrint:
    """embed(audio) -> unit-length 256-float vector, or None when there is too little audio."""

    def __init__(self, model_path: Path | str = MODEL_PATH, threads: int = 2, keep: float = 1.0):
        self.keep = keep
        import onnxruntime as ort                                         # present on the station (faster-whisper)
        o = ort.SessionOptions()
        o.intra_op_num_threads = threads                                  # never starve the live transcription
        o.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model_path), o, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name

    @staticmethod
    def available(model_path: Path | str = MODEL_PATH) -> bool:
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        return Path(model_path).is_file()

    def embed(self, audio: np.ndarray, sr: int = SR) -> np.ndarray | None:
        if len(audio) < MIN_S * sr:
            return None
        e = self.sess.run(None, {self.inp: fbank(audio, sr, self.keep)[None]})[0][0].astype(np.float32)
        nrm = float(np.linalg.norm(e))
        return e / nrm if nrm > 0 else None

    def embed_file(self, path: Path | str) -> np.ndarray | None:
        import soundfile as sf
        a, sr = sf.read(str(path), dtype="float32", always_2d=True)
        a = a.mean(axis=1)
        if sr != SR:                                                      # station WAVs are 16 kHz; be safe anyway
            a = np.interp(np.arange(0, len(a), sr / SR), np.arange(len(a)), a).astype(np.float32)
        return self.embed(a)
