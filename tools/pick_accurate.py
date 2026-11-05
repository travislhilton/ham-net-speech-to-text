"""Benchmark the ACCURATE pass on this PC (run by the installer after tools/pick_engine.py) and decide what the PC
can sustain on its own when the home server is not there: both stages / the final stage only / none.

For each accurate model present (OpenVINO whisper-large-v3-int8-ov, whisper-large-v3-turbo-int8-ov; faster-whisper
large-v3 on an NVIDIA card) on each device (Intel GPU, NPU, CPU), in a child process each (a GPU pipeline must not
share a process with an openvino.Core, and its unload can hang: the child is ended with TerminateProcess), the same
decoder the station runs (transcribe/decoders.py: word timestamps, 30 s windows) decodes a ~55 s test block built
from install/windows/bench.wav (a synthetic voice, at three speeds, so Whisper does not skip repeats). rtf = seconds
of work per second of audio for ONE decode; peak memory of the child is the model's RAM.

Decision (station/accurate_local.decide): large-v3 when a device runs it at rtf <= 1.0, else turbo, else none;
a device the live final is not on wins when it is within 2x of the fastest (no contention); provisional too only at
rtf <= 0.12. Written to config/engine.yaml as `accurate:` (pick_engine.py keeps it).

    python tools/pick_accurate.py [--models DIR] [--out config/engine.yaml] [--devices GPU,CPU] [--quick]
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CLIP = ROOT / "install" / "windows" / "bench.wav"
SR = 16000
OV_MODELS = ("whisper-large-v3-int8-ov", "whisper-large-v3-turbo-int8-ov")


def test_block(clip=CLIP, speeds=(1.0, 0.9, 1.1, 0.95, 1.05, 0.85)) -> np.ndarray:
    """~55 s: the bench clip at several speeds with short gaps (a repeated identical clip is read once and the rest
    skipped, which would make a slow device look fast)."""
    import soundfile as sf
    a = sf.read(clip, dtype="float32")[0]
    a = a.mean(axis=1) if a.ndim > 1 else a
    parts = []
    for i, s in enumerate(speeds):
        n = int(len(a) / s)
        parts.append(np.interp(np.arange(n) * s, np.arange(len(a)), a).astype(np.float32) * (0.8 + 0.1 * (i % 3)))
        parts.append(np.zeros(int(SR * (0.5 + 0.2 * (i % 2))), np.float32))
    return np.concatenate(parts)


def peak_mb() -> int | None:
    """This process's peak private memory (Windows) or max RSS (elsewhere), MB."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                           [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize",
                            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]
            k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            c = PMC(); c.cb = ctypes.sizeof(c)
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return int(max(c.PeakPagefileUsage, c.PeakWorkingSetSize) >> 20)
            return None
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(r >> 20) if sys.platform == "darwin" else int(r >> 10)
    except Exception:  # noqa: BLE001
        return None


def measure_one(spec: dict, models: Path, quick=False) -> dict:
    """In the child: load, warm up (GPU kernels compile on the first window), time one decode of the test block."""
    from transcribe import decoders
    x = test_block()
    if quick:
        x = x[: SR * 29]
    t0 = time.time()
    # no temperature fallback: the test block repeats one sentence, which the loop guard would retry 6 times
    dec = decoders.make(spec, models, cache_dir=str(ROOT / "data" / "ov-cache"), fallback=False)
    load = time.time() - t0
    dec(x[: SR * 10])
    t0 = time.time()
    segs = dec(x)
    el = time.time() - t0
    text = " ".join((s.get("text") or "").strip() for s in segs)
    words = sum(len(s.get("words") or []) for s in segs)
    return {"load_s": round(load, 1), "rtf": round(el / (len(x) / SR), 3), "audio_s": round(len(x) / SR, 1),
            "words": words, "word_times": words > 0,
            "ram_mb": peak_mb(), "text": text[:160]}


def openvino_devices() -> list[str]:
    from tools.pick_engine import openvino_devices as devs          # asks in a child process (see there)
    return devs()


def has_nvidia() -> bool:
    from tools.pick_engine import has_nvidia as nv
    return nv()


def candidates(models: Path, devices=None) -> list[dict]:
    out = []
    devs = devices or openvino_devices()
    # No NPU: large-v3 and turbo did not finish compiling for it in 20 min each (test laptop, Intel NPU, 2026-11-04),
    # which only added 40 min to the install. The live words-while-talking model uses it instead (pick_engine.py).
    order = [d for d in devs if d.startswith("GPU")] + [d for d in devs if d == "CPU"]
    for name in OV_MODELS:
        if (models / name).exists():
            out += [{"engine": "openvino", "model": name, "device": d} for d in order]
    if has_nvidia():
        out.append({"engine": "faster-whisper", "model": "faster-whisper-large-v3"
                    if (models / "faster-whisper-large-v3").exists() else "large-v3", "device": "cuda"})
    return out


def run_child(spec: dict, models: Path, quick: bool, timeout=1200) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--one", json.dumps(spec), "--models", str(models)] + \
          (["--quick"] if quick else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout} s"}
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {"error": (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=str(ROOT / "models"))
    ap.add_argument("--out", default=str(ROOT / "config" / "engine.yaml"))
    ap.add_argument("--devices", help="comma separated, instead of asking OpenVINO (e.g. GPU,CPU)")
    ap.add_argument("--quick", action="store_true", help="one 29 s window instead of ~55 s")
    ap.add_argument("--one", help=argparse.SUPPRESS)                  # the child: measure this spec, print JSON
    a = ap.parse_args()
    models = Path(a.models)
    if a.one:
        try:
            print(json.dumps(measure_one(json.loads(a.one), models, a.quick)), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {str(e)[:200]}"}), flush=True)
        return 0
    from station.accurate_local import decide
    out = Path(a.out)
    choice = (yaml.safe_load(out.read_text()) if out.exists() else None) or {}
    live = (choice.get("final") or {}).get("device")
    rows = []
    from station.awake import KeepAwake              # measure at full speed: laptops crawl with the screen dark
    awake = KeepAwake("HamNet accurate-pass benchmark", snapshot=ROOT / "data" / "power_snapshot_bench.json")
    awake.set(True)
    try:
        for c in candidates(models, a.devices.split(",") if a.devices else None):
            r = run_child(c, models, a.quick)
            row = c | r
            rows.append(row); print(json.dumps(row), flush=True)
    finally:
        awake.set(False)
    pick = decide(rows, live)
    choice["accurate"] = pick | {"measured": rows, "note": "written by tools/pick_accurate.py; re-run after hardware "
                                                         "or driver changes (rtf = work s per audio s, one decode)"}
    out.write_text(yaml.safe_dump(choice, sort_keys=False))
    print("\nACCURATE:", {k: v for k, v in pick.items()}, "\n->", out)
    return 0


if __name__ == "__main__":
    from tools.pick_engine import hard_exit
    hard_exit(main())
