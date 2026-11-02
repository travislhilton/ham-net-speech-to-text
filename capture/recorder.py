#!/usr/bin/env python3
"""Continuous VAD/COS-gated recorder for ham voice nets.

Writes one WAV + JSON sidecar per transmission into out_dir/YYYYmmdd/, then hands the
pair to uploader.enqueue(). Runs forever; Ctrl-C or service stop to exit.
"""
import argparse, collections, json, queue, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import yaml

import uploader
from gates import CarrierGate

FRAME_MS = 32  # 512 samples @16k — Silero's native window


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"{i:3d}  {d['name']}  ({d['hostapi']}) in={d['max_input_channels']}")


def find_device(name_sub):
    for i, d in enumerate(sd.query_devices()):
        if name_sub.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    raise SystemExit(f"input device containing '{name_sub}' not found; use --list-devices")


class SileroGate:
    def __init__(self, cfg):
        from silero_vad import load_silero_vad
        import torch
        torch.set_num_threads(2)
        self.model = load_silero_vad()
        self.th = cfg["threshold"]

    def is_speech(self, frame16):
        import torch
        return float(self.model(torch.from_numpy(frame16), 16000)) >= self.th


class CosGate:
    """Carrier-operated squelch via serial CTS (Digirig exposes it)."""
    def __init__(self, cfg):
        import serial
        self.ser = serial.Serial(cfg["port"])
        self.inv = cfg.get("invert", False)

    def is_speech(self, _frame):
        return self.ser.cts != self.inv


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sr = cfg["sample_rate"]
        self.frame = int(self.sr * FRAME_MS / 1000)
        mode = cfg["vad"]["mode"]
        if mode == "carrier":
            self.gate = CarrierGate(cfg.get("carrier", {}), self.sr)
        elif mode == "cos":
            self.gate = CosGate(cfg["cos"])
        else:
            self.gate = SileroGate(cfg["vad"])
        self.q = queue.Queue()
        self.out = Path(cfg["out_dir"])
        v = cfg["vad"]
        self.min_frames = v["min_speech_ms"] // FRAME_MS
        self.hang_frames = v["hangover_ms"] // FRAME_MS
        self.max_frames = int(v["max_tx_s"] * 1000 // FRAME_MS)
        self.pre = collections.deque(maxlen=max(1, v.get("preroll_ms", 250) // FRAME_MS))

    def _cb(self, indata, frames, t, status):
        if status:
            print("audio:", status, file=sys.stderr)
        self.q.put(indata[:, 0].copy())

    def run(self):
        dev = find_device(self.cfg["device_name"])
        print(f"listening on device {dev} @ {self.sr} Hz")
        buf, silent, active = [], 0, False
        started = None
        with sd.InputStream(device=dev, samplerate=self.sr, channels=1, dtype="float32",
                            blocksize=self.frame, callback=self._cb):
            while True:
                frame = self.q.get()
                speech = self.gate.is_speech(frame)
                if not active:
                    if speech:
                        # keep the audio just before the gate opened (first syllable)
                        active, silent, buf = True, 0, list(self.pre) + [frame]
                        started = datetime.now(timezone.utc)
                    self.pre.append(frame)
                    continue
                buf.append(frame)
                silent = 0 if speech else silent + 1
                if silent >= self.hang_frames or len(buf) >= self.max_frames:
                    self._close(buf, started, len(buf) - silent)
                    active, buf = False, []

    def _close(self, buf, started, speech_frames):
        if speech_frames < self.min_frames:
            return  # kerchunk / squelch tail
        audio = np.concatenate(buf)
        day = started.strftime("%Y%m%d")
        d = self.out / day
        d.mkdir(parents=True, exist_ok=True)
        stem = started.strftime("%H%M%S_%f")[:-3]
        wav = d / f"{stem}.wav"
        sf.write(wav, audio, self.sr, subtype="PCM_16")
        meta = {
            "started_utc": started.isoformat(),
            "duration_s": round(len(audio) / self.sr, 2),
            "sample_rate": self.sr,
            "frequency_mhz": self.cfg["frequency_mhz"],
            "net_name": self.cfg["net_name"],
            "station": self.cfg["station"],
            "gate": self.cfg["vad"]["mode"],
            "host": "puck",
        }
        wav.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        print(f"tx {stem} {meta['duration_s']}s")
        uploader.enqueue(wav)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--list-devices", action="store_true")
    a = ap.parse_args()
    if a.list_devices:
        list_devices(); return
    cfg = yaml.safe_load(open(a.config))
    uploader.start(cfg)
    Recorder(cfg).run()


if __name__ == "__main__":
    main()
