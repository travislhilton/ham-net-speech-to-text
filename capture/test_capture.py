"""Test capture from NET-REC: streams to disk as it records, so stopping early keeps the audio.
Usage: rec.py [seconds=30] [out=C:\\HAM-Sidekick-Station\\setup\\test.wav]"""
import sys, numpy as np, sounddevice as sd, soundfile as sf
secs = int(sys.argv[1]) if len(sys.argv) > 1 else 30
path = sys.argv[2] if len(sys.argv) > 2 else r"C:\HAM-Sidekick-Station\setup\test.wav"
dev = next(i for i, d in enumerate(sd.query_devices()) if "net-rec" in d["name"].lower() and d["max_input_channels"] > 0)
db = lambda v: 20 * np.log10(max(float(v), 1e-9))
per_sec, peak = [], 0.0
with sf.SoundFile(path, "w", samplerate=16000, channels=1, subtype="PCM_16") as f, \
     sd.InputStream(device=dev, samplerate=16000, channels=1, dtype="float32", blocksize=16000) as s:
    try:
        for _ in range(secs):
            blk, _ = s.read(16000); blk = blk[:, 0]; f.write(blk); f.flush()
            per_sec.append(db(np.sqrt((blk**2).mean()))); peak = max(peak, float(np.abs(blk).max()))
    except KeyboardInterrupt:
        pass
print(f"device {dev}; {len(per_sec)} s; peak {db(peak):.1f} dBFS")
print("per-second rms dBFS:", " ".join(f"{v:.0f}" for v in per_sec))
