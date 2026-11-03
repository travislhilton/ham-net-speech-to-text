"""The one place a WAV becomes a transcript. Used by the HTTP service (transcribe/server.py) and by
analyze/retranscribe.py, so a re-run produces exactly what a live upload would have."""
import os, time, wave
from pathlib import Path

import numpy as np

from . import callsigns

MODEL = os.environ.get("STT_MODEL", "mlx-community/whisper-large-v3-turbo")
HOST = __import__("socket").gethostname().split(".")[0].lower()


def load_audio(path: Path):
    """16 kHz mono 16-bit WAV (what capture/ produces) is decoded here with no ffmpeg dependency;
    anything else is handed to mlx_whisper as a path and decoded by ffmpeg."""
    try:
        with wave.open(str(path)) as w:
            if w.getnchannels() == 1 and w.getframerate() == 16000 and w.getsampwidth() == 2:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                return pcm.astype(np.float32) / 32768.0
    except wave.Error:
        pass
    return str(path)


class Engine:
    """Holds the vocabulary-derived prompt and roster; reload() picks up a changed local.yaml."""

    def __init__(self, model: str = MODEL):
        self.model = model
        self.reload()

    def reload(self):
        self.prompt = callsigns.initial_prompt()
        self.roster = callsigns.load_roster()

    def transcribe(self, wav: Path) -> dict:
        import mlx_whisper  # heavy import; keeps tests and tooling light
        t0 = time.time()
        r = mlx_whisper.transcribe(load_audio(wav), path_or_hf_repo=self.model, language="en",
                                   initial_prompt=self.prompt, word_timestamps=True,
                                   condition_on_previous_text=False)
        text, found = callsigns.normalize(r["text"].strip(), self.roster)
        return {
            "text": text,
            "callsigns": found,
            "segments": [{"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                          "avg_logprob": s.get("avg_logprob"), "no_speech_prob": s.get("no_speech_prob")}
                         for s in r["segments"]],
            "model": self.model, "host": HOST, "elapsed_s": round(time.time() - t0, 2),
            "wav": str(wav),
        }
