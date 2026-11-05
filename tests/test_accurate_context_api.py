"""POST /accurate/context with mlx_whisper stubbed (no database, no GPU): several WAVs + a manifest in, per-clip text
out; context clips are heard but not returned; auth and bad input refused."""
import io, json, sys, types, wave

import numpy as np
import pytest

pytest.importorskip("fastapi", reason="home-server tests (transcribe/requirements.txt)")


def _wav(seconds, hz=800, amp=0.3):
    t = np.arange(int(seconds * 16000)) / 16000
    x = (amp * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(x.tobytes())
    return b.getvalue()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("EMCOMM_DB_DSN", raising=False)
    monkeypatch.setenv("EMCOMM_STT_KEY", "k")
    monkeypatch.setenv("STT_DATA_DIR", str(tmp_path))
    calls = []

    def fake_transcribe(x, **kw):
        # "hears" one word per second of tone in the window, named by the window's call count
        calls.append({"n": len(x), **kw})
        env = np.convolve(np.abs(x), np.ones(160) / 160, "same") > 0.05
        words, i = [], 0
        while i < len(env):
            if env[i]:
                j = i
                while j < len(env) and env[j]:
                    j += 1
                words.append({"word": f" w{len(words)}", "start": i / 16000, "end": j / 16000, "probability": 0.9})
                i = j
            else:
                i += 1
        return {"text": "", "segments": [{"text": "".join(w["word"] for w in words), "avg_logprob": -0.2,
                                          "no_speech_prob": 0.01, "compression_ratio": 1.1, "words": words}]}
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake_transcribe))
    import importlib, transcribe.server as server
    server = importlib.reload(server)
    from fastapi.testclient import TestClient
    c = TestClient(server.app, headers={"X-API-Key": "k"})
    c.calls = calls
    return c


def _post(c, clips, stage="final", **kw):
    files = {f"a{i}": (f"{cl['id']}.wav", cl.pop("wav"), "audio/wav") for i, cl in enumerate(clips)}
    for i, cl in enumerate(clips):
        cl["file"] = f"a{i}"
    man = {"clips": clips, "highpass_hz": 300, "stage": stage, **kw}
    return c.post("/accurate/context", data={"manifest": json.dumps(man)}, files=files)


def test_context_endpoint_maps_words_back_per_clip(client):
    r = _post(client, [{"id": "1", "start": "2026-11-02T20:00:00", "wav": _wav(1.0), "target": False},
                       {"id": "2", "start": "2026-11-02T20:00:05", "wav": _wav(1.0)},
                       {"id": "3", "start": "2026-11-02T20:00:08", "wav": _wav(1.0)}])
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d["results"]) == {"2", "3"}                     # the context clip is not returned
    assert d["results"]["2"]["text"] == "w1" and d["results"]["3"]["text"] == "w2"   # one window: clip 1 was w0
    assert d["stage"] == "final" and d["audio_s"] == 3.0 and d["model"] == "whisper-large-v3-mlx"
    kw = client.calls[0]
    assert kw["word_timestamps"] and kw["temperature"][0] == 0 and not kw["condition_on_previous_text"]
    assert kw["initial_prompt"] is None and kw["no_speech_threshold"] is None   # FM hiss: never skip a window
    assert len(client.calls) == 2 and client.calls[0]["n"] == 4 * 16000   # final: the long decode + one window (all 4 s)


def test_provisional_decodes_once(client):
    r = _post(client, [{"id": "9", "start": 1000.0, "wav": _wav(2.0)}], stage="provisional")
    assert r.status_code == 200 and r.json()["results"]["9"]["text"] == "w0" and len(client.calls) == 1


def test_context_endpoint_refuses_bad_input(client):
    from fastapi.testclient import TestClient
    import transcribe.server as server
    assert TestClient(server.app).post("/accurate/context", data={"manifest": "{}"}).status_code == 401
    assert client.post("/accurate/context", data={"manifest": "not json"}).status_code == 422
    assert client.post("/accurate/context", data={"manifest": json.dumps({"clips": []})}).status_code == 422
    r = client.post("/accurate/context", data={"manifest": json.dumps({"clips": [{"id": "1", "start": 0, "file": "nope"}]})})
    assert r.status_code == 422
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\0\0\0\0" * 100)
    assert _post(client, [{"id": "1", "start": 0, "wav": b.getvalue()}]).status_code == 415
