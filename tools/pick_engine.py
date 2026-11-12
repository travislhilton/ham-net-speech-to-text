"""Benchmark the Whisper engines this PC can run and choose the live configuration.

Windows stations differ (Intel iGPU, NVIDIA, AMD, CPU only), so the installer measures rather
than assumes. Clip: install/windows/bench.wav (a synthetic voice, band-limited like a radio, saying K7ABC).

Choice: FINAL = fastest engine running large-v3-turbo at <= 0.6x real time (accuracy matched the
home server in testing); else small.en. PARTIAL (words while someone is still talking) = base.en if
it runs at <= 0.15x real time, else none. Writes config/engine.yaml.

    python tools/pick_engine.py [--models DIR] [--quick]
"""
import argparse, json, os, shutil, subprocess, sys, time
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hamkit.callsigns import initial_prompt, normalize  # noqa: E402  (same prompt + normalizer as live)
PROMPT = initial_prompt()
CLIP = ROOT / "install" / "windows" / "bench.wav"
EXPECT = "K7ABC"


def openvino_runner(model_dir: Path, device: str):
    import openvino_genai as ov
    cache = ROOT / "data" / "ov-cache"          # the live engine's cache: a GPU/NPU compile done here is reused
    cache.mkdir(parents=True, exist_ok=True)
    pipe = ov.WhisperPipeline(str(model_dir), device, CACHE_DIR=str(cache))
    cfg = pipe.get_generation_config(); cfg.max_new_tokens = 200
    if ".en-" not in model_dir.name:
        cfg.language = "<|en|>"; cfg.task = "transcribe"
    cfg.initial_prompt = PROMPT
    return lambda a: str(pipe.generate(a, cfg))


def cuda_runner(model: str):
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cuda", compute_type="float16")
    return lambda a: " ".join(s.text for s in m.transcribe(a, language="en", beam_size=1, initial_prompt=PROMPT)[0])


def has_nvidia() -> bool:
    exe = shutil.which("nvidia-smi")
    return bool(exe) and subprocess.run([exe, "-L"], capture_output=True).returncode == 0


def openvino_devices() -> list[str]:
    """Ask in a child process. An openvino.Core that enumerated the GPU, plus a GPU WhisperPipeline in
    the same process, hangs that process forever at exit (test PC, Intel UHD, driver 32.0.101.7088,
    OpenVINO 2026.4.1; either alone exits cleanly). The live service must never create a Core either."""
    r = subprocess.run([sys.executable, "-c", "import openvino as o; print(','.join(o.Core().available_devices))"],
                       capture_output=True, text=True, timeout=120)
    return [d for d in r.stdout.strip().split(",") if d] if r.returncode == 0 else []


def candidates(models: Path):
    out = []
    devs = openvino_devices()
    for name, role in (("whisper-large-v3-turbo-int8-ov", "final"), ("whisper-small.en-int8-ov", "final"),
                       ("whisper-base.en-int8-ov", "partial")):
        if (models / name).exists():
            # No NPU (test laptop, Intel NPU, OpenVINO 2026.4, 2026-11-04): its static Whisper pipeline refuses the
            # station's callsign prompt ("roi_end <= max_dim"), first compiles take 3-9 min (turbo did not finish in
            # 30), and at full speed the GPU ran turbo at 0.07x real time and base.en at 0.03x anyway.
            for d in [x for x in devs if x.startswith("GPU")] + (["CPU"] if "CPU" in devs else []):
                out.append({"engine": "openvino", "device": d, "model": name, "role": role,
                            "make": (lambda n=name, dv=d: openvino_runner(models / n, dv))})
    if has_nvidia():
        out.append({"engine": "faster-whisper", "device": "cuda", "model": "large-v3-turbo", "role": "final",
                    "make": lambda: cuda_runner("large-v3-turbo")})
    return out


def measure(c, audio):
    t0 = time.time(); run = c["make"](); load = time.time() - t0
    run(audio)                                   # warm-up: GPU kernels compile on the first call
    times, text = [], ""
    for _ in range(2):
        t0 = time.time(); text = run(audio); times.append(time.time() - t0)
    rtf = min(times) / (len(audio) / 16000)
    return {"load_s": round(load, 1), "rtf": round(rtf, 3), "text": text.strip(),
            "heard_call": EXPECT in normalize(text, roster={EXPECT})[1]}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--models", default=str(ROOT / "models"))
    ap.add_argument("--out", default=str(ROOT / "config" / "engine.yaml")); a = ap.parse_args()
    audio = sf.read(CLIP, dtype="float32")[0]
    from station.awake import KeepAwake              # measure at full speed: laptops crawl with the screen dark
    # Under station.pccheck measure (HAMNET_PLAN_HELD=1) the power plan is already held and saved: only the screen
    # request here, or this would see the HamNet plan with nothing saved and put Balanced back mid-measurement.
    awake = KeepAwake("HamNet engine benchmark", snapshot=None if os.environ.get("HAMNET_PLAN_HELD") else
                      ROOT / "data" / "power_snapshot_bench.json")
    awake.set(True)
    try:
        return _measure_all(a, audio)
    finally:
        awake.set(False)


def _measure_all(a, audio):
    results = []
    for c in candidates(Path(a.models)):
        try:
            r = measure(c, audio)
        except Exception as e:  # noqa: BLE001 — an engine this PC can't run is just skipped
            r = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        row = {k: c[k] for k in ("engine", "device", "model", "role")} | r
        results.append(row); print(json.dumps(row), flush=True)
    ok = [r for r in results if "rtf" in r]
    turbo = [r for r in ok if "turbo" in r["model"] and r["rtf"] <= 0.6]
    small = [r for r in ok if "small" in r["model"]]
    final = min(turbo or small or [r for r in ok if r["role"] == "final"] or [None],
                key=lambda r: r["rtf"] if r else 0)
    part = [r for r in ok if r["role"] == "partial" and r["rtf"] <= 0.15]
    partial = min(part, key=lambda r: r["rtf"]) if part else None
    pick = lambda r: r and {k: r[k] for k in ("engine", "device", "model", "rtf")}
    choice = {"final": pick(final), "partial": pick(partial), "measured": results,
              "note": "written by tools/pick_engine.py; re-run after hardware or driver changes"}
    try:                                     # the accurate pass's own measurement (tools/pick_accurate.py) stays
        old = yaml.safe_load(Path(a.out).read_text(encoding="utf-8-sig")) or {}
        if old.get("accurate"):
            choice["accurate"] = old["accurate"]
    except (OSError, yaml.YAMLError):
        pass
    Path(a.out).write_text(yaml.safe_dump(choice, sort_keys=False))
    print("\nFINAL  :", pick(final)); print("PARTIAL:", pick(partial)); print("->", a.out)
    return 0 if final else 1


def hard_exit(rc: int):
    """Leave without running library unload code. OpenVINO's GPU plugin deadlocks while unloading on
    Windows (test PC, 2026-11-02): normal exit AND os._exit both spun one core for 10+ minutes after
    engine.yaml was written, because ExitProcess still calls every DLL's detach routine.
    TerminateProcess skips them."""
    sys.stdout.flush(); sys.stderr.flush()
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.TerminateProcess(k32.GetCurrentProcess(), rc)
    os._exit(rc)


if __name__ == "__main__":
    hard_exit(main())
