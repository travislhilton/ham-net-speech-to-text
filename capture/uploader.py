"""Background uploader: POST clip to home server; on failure queue it and optionally
transcribe locally with faster-whisper so a transcript still exists during an outage."""
import json, os, shutil, threading, time
from pathlib import Path
import requests

_cfg = None
_fails = 0
_local = None


def start(cfg):
    global _cfg
    _cfg = cfg
    Path(cfg["queue_dir"]).mkdir(exist_ok=True)
    threading.Thread(target=_drain, daemon=True).start()


def enqueue(wav: Path):
    if not _post(wav):
        dst = Path(_cfg["queue_dir"]) / wav.name
        shutil.copy2(wav, dst)
        shutil.copy2(wav.with_suffix(".json"), dst.with_suffix(".json"))
        _maybe_local(wav)


def _post(wav: Path) -> bool:
    global _fails
    up = _cfg["upload"]
    meta = json.loads(wav.with_suffix(".json").read_text())
    try:
        r = requests.post(up["url"],
                          headers={"X-API-Key": os.environ.get(up["api_key_env"], "")},
                          files={"audio": (wav.name, open(wav, "rb"), "audio/wav")},
                          data={"meta": json.dumps(meta)}, timeout=up["timeout_s"])
        r.raise_for_status()
        _fails = 0
        return True
    except Exception as e:
        _fails += 1
        print(f"upload failed ({_fails}): {e}")
        return False


def _maybe_local(wav: Path):
    global _local
    fb = _cfg["fallback"]
    if not fb["enabled"] or _fails < fb["after_failures"]:
        return
    if _local is None:
        from faster_whisper import WhisperModel
        _local = WhisperModel(fb["model"], device="cpu", compute_type="int8")
    # Decode ourselves: faster-whisper 1.2.1's path decoding calls PyAV with an argument newer
    # PyAV rejects ("metadata_errors"), seen on W9XYZ-5 2026-11-02.
    import soundfile as sf
    audio, _sr = sf.read(str(wav), dtype="float32")
    segs, _ = _local.transcribe(audio, language="en", vad_filter=False)
    text = " ".join(s.text.strip() for s in segs)
    side = wav.with_suffix(".local.txt")
    side.write_text(text)
    print(f"local transcript: {text[:80]}")


def _drain():
    while True:
        time.sleep(_cfg["upload"]["retry_interval_s"])
        for wav in sorted(Path(_cfg["queue_dir"]).glob("*.wav")):
            if _post(wav):
                wav.unlink(); wav.with_suffix(".json").unlink(missing_ok=True)
            else:
                break
