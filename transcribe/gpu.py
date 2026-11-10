"""Whisper holds the GPU only while it is used.

Nets do not run all the time, so the home server loads Whisper when a request needs it and gives the memory back
when the requests stop:

* LOAD ON DEMAND. mlx_whisper loads the model on the first decode (its `ModelHolder`); nothing is loaded at start-up.
* CACHE RELEASED AFTER EVERY REQUEST. MLX keeps the GPU buffers a decode frees for reuse, and it never hands them
  back by itself. A final pass (up to 15 min of audio, Whisper's long decode AND our windows, word timestamps) frees
  buffers of a new shape each time, so the cache only grew: measured 2026-11-10, six replayed final passes took the
  process from 0.25 GB to 20 GB (16 GB of it cache), and the live service had reached 43 GB in 12 h, filled swap and
  stalled every other GPU job on the host. With the cap and a clear after each request the same six stayed at
  4.5 GB, no slower.
* MODEL UNLOADED WHEN IDLE. After `STT_IDLE_UNLOAD_S` (default 600 s) with no decode, the model is dropped and the
  process returns to its start-up size; the next request loads it again (a few seconds, from the local cache).
  A net sends a request every few seconds, so it never unloads mid-net. 0 keeps the model loaded.

`using()` is also the one-Whisper-at-a-time lock every decode on this host goes through.
"""
from __future__ import annotations

import gc
import os
import threading
import time
from contextlib import contextmanager

IDLE_UNLOAD_S = float(os.environ.get("STT_IDLE_UNLOAD_S", "600"))
CACHE_LIMIT_GB = float(os.environ.get("STT_MLX_CACHE_GB", "4"))
CHECK_EVERY_S = 60.0

lock = threading.Lock()
_last_used = 0.0          # monotonic seconds of the last decode's end; 0 = never used since start-up
_watcher: threading.Thread | None = None


def _mx():
    """mlx.core, or None where MLX is not installed (a station PC, a test with mlx_whisper stubbed)."""
    try:
        import mlx.core as mx
        return mx
    except ImportError:
        return None


def _holder():
    """mlx_whisper's one-model cache (`ModelHolder`), or None when mlx_whisper is absent or stubbed."""
    try:
        from mlx_whisper.transcribe import ModelHolder
        return ModelHolder
    except Exception:  # noqa: BLE001 - absent, or a stub without the submodule
        return None


def configure() -> None:
    """Cap the cache MLX keeps between decodes. Called once at start-up."""
    mx = _mx()
    if mx is not None and CACHE_LIMIT_GB >= 0:
        mx.set_cache_limit(int(CACHE_LIMIT_GB * 2**30))


def release_cache() -> None:
    mx = _mx()
    if mx is not None:
        mx.clear_cache()


@contextmanager
def using():
    """Run one decode: one Whisper on the GPU at a time, the cache handed back after it, the idle clock restarted."""
    global _last_used
    with lock:
        try:
            yield
        finally:
            release_cache()
            _last_used = time.monotonic()


def loaded() -> bool:
    h = _holder()
    return bool(h is not None and getattr(h, "model", None) is not None)


def idle_s(now: float | None = None) -> float | None:
    """Seconds since the last decode ended, or None if none has run since start-up."""
    if not _last_used:
        return None
    return (time.monotonic() if now is None else now) - _last_used


def unload_if_idle(now: float | None = None) -> bool:
    """Drop the model when nothing has decoded for IDLE_UNLOAD_S. True when it was unloaded.
    Takes the decode lock, so it never pulls the model out from under a running decode."""
    if IDLE_UNLOAD_S <= 0:
        return False
    with lock:
        idle = idle_s(now)
        h = _holder()
        if idle is None or idle < IDLE_UNLOAD_S or h is None or getattr(h, "model", None) is None:
            return False
        h.model = None
        h.model_path = None
        gc.collect()
        release_cache()
        return True


def start_idle_watcher() -> None:
    """A daemon thread that checks once a minute. Idempotent."""
    global _watcher
    if IDLE_UNLOAD_S <= 0 or (_watcher is not None and _watcher.is_alive()):
        return

    def run():
        while True:
            time.sleep(CHECK_EVERY_S)
            try:
                if unload_if_idle():
                    print(f"gpu: Whisper unloaded after {IDLE_UNLOAD_S:.0f} s idle", flush=True)
            except Exception as e:  # noqa: BLE001 - the watcher must outlive one bad check
                print(f"gpu: idle check failed: {e!r}", flush=True)

    _watcher = threading.Thread(target=run, name="whisper-idle-unload", daemon=True)
    _watcher.start()


def status() -> dict:
    mx = _mx()
    out = {"whisper_loaded": loaded(), "idle_unload_s": IDLE_UNLOAD_S}
    idle = idle_s()
    out["idle_s"] = round(idle) if idle is not None else None
    if mx is not None:
        out["gpu_active_gb"] = round(mx.get_active_memory() / 2**30, 2)
        out["gpu_cache_gb"] = round(mx.get_cache_memory() / 2**30, 2)
    return out
