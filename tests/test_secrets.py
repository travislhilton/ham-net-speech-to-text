"""station/secrets.py + /api/secrets: each owner supplies their own key; the page never receives it."""
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from station import secrets

KEY = "xai-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


def test_write_read_replace_remove_and_keep_other_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    (tmp_path / "config").mkdir()
    secrets.path(tmp_path).write_text("EMCOMM_STT_KEY=abc123\nXAI_API_KEY=\n", encoding="utf-8")
    assert secrets.status(tmp_path, "XAI_API_KEY")["set"] is False
    s = secrets.write(tmp_path, "XAI_API_KEY", "XAI_API_KEY=" + KEY)        # a pasted whole line is fine
    assert s == {"name": "XAI_API_KEY", "label": "xAI API key", "set": True, "hint": "…M3n4", "from": "file"}
    assert secrets.read(tmp_path, "XAI_API_KEY") == KEY and secrets.read(tmp_path, "EMCOMM_STT_KEY") == "abc123"
    text = secrets.path(tmp_path).read_text(encoding="utf-8")
    assert text.count("XAI_API_KEY=") == 1 and "EMCOMM_STT_KEY=abc123" in text
    secrets.write(tmp_path, "XAI_API_KEY", None)
    assert secrets.read(tmp_path, "XAI_API_KEY") is None and secrets.read(tmp_path, "EMCOMM_STT_KEY") == "abc123"


def test_a_key_of_the_wrong_shape_or_name_is_refused(tmp_path):
    with pytest.raises(ValueError, match="doesn't look like"):
        secrets.write(tmp_path, "XAI_API_KEY", "sk-not-xai")
    with pytest.raises(ValueError, match="unknown"):
        secrets.write(tmp_path, "EMCOMM_STT_KEY", "x")


def test_the_test_call_sends_the_key_and_explains_a_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    secrets.write(tmp_path, "XAI_API_KEY", KEY)
    seen = {}

    def ok(req, timeout):
        seen["auth"] = req.headers["Authorization"]
        return io.BytesIO(json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode())
    assert secrets.test(tmp_path, "XAI_API_KEY", opener=ok) == {"ok": True, "say": "The key works (2 models available)."}
    assert seen["auth"] == "Bearer " + KEY

    def refused(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    r = secrets.test(tmp_path, "XAI_API_KEY", opener=refused)
    assert not r["ok"] and "refused" in r["say"]


def test_the_dashboard_routes_never_return_the_key(tmp_path, monkeypatch):
    import copy
    from station import engine
    from station.core import Station
    from station.server import serve
    from tests.test_cat import CFG
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    st = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path / "data")
    st.root = tmp_path
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda p, b: urllib.request.urlopen(urllib.request.Request(
        base + p, json.dumps(b).encode(), {"Content-Type": "application/json"})).read().decode()
    try:
        out = post("/api/secrets/XAI_API_KEY", {"value": KEY})
        listed = urllib.request.urlopen(base + "/api/secrets").read().decode()
        assert KEY not in out and KEY not in listed and '"set": true' in listed and "M3n4" in listed
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/secrets/OTHER_KEY", {"value": "x"})
        assert e.value.code == 404
    finally:
        srv.shutdown()
