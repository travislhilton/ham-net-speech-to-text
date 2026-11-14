"""AI costs (owner 2026-11-14): the ledger of every paid call, the monthly budget, the xAI account's credit and spend
through a management key, and the Buy credits link (station/aicost.py)."""
import io
import json
import threading
import urllib.error
from datetime import datetime

import pytest

from station import agent, aicost, netfind, netsummary
from station.server import serve
from tests.test_history import _post
from tests.test_netsummary import KEY, _station

NOW = datetime(2026, 11, 3, 21, 0)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _answer(usage, content='{"say": "ok"}'):
    return _Resp(json.dumps({"model": "grok-4.20-0309-non-reasoning", "usage": usage,
                             "choices": [{"message": {"content": content}}]}).encode())


def test_cost_uses_xais_own_figure_else_the_price_list():
    assert aicost.cost("grok-4.20-0309-non-reasoning", {"prompt_tokens": 1000, "completion_tokens": 100,
                                                        "cost_in_usd_ticks": 15_000_000}) == (0.0015, "xai")
    usd, how = aicost.cost("grok-4.20-0309-non-reasoning",
                           {"prompt_tokens": 100_000, "prompt_tokens_details": {"cached_tokens": 50_000},
                            "completion_tokens": 10_000})
    assert how == "est" and usd == pytest.approx((50_000 * 1.25 + 50_000 * 0.20 + 10_000 * 2.50) / 1e6)
    usd, _ = aicost.cost("grok-4.20-0309-non-reasoning", {"input_tokens": 0, "output_tokens": 0,
                                                          "server_side_tool_usage_details": {"web_search_calls": 10}})
    assert usd == pytest.approx(0.05)                                     # $5 per 1000 searches
    assert aicost.cost("", None, audio_s=3600) == (pytest.approx(0.10), "est")
    assert aicost.cost("", None, chars=1_000_000) == (pytest.approx(15.0), "est")
    assert aicost.cost("claude-haiku-5-5", {"prompt_tokens": 10}) == (None, "none")   # counted, not priced
    long = aicost.cost("grok-4.3", {"prompt_tokens": 200_000, "completion_tokens": 0})[0]
    assert long == pytest.approx(2 * 0.2 * 1.25)                          # 200k+ prompt: all at double


def test_every_xai_call_goes_in_the_ledger(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    st.costs                                          # the running station's ledger (start() makes it)
    c = st.costs
    monkeypatch.setattr(aicost.urllib.request, "urlopen",
                        lambda req, timeout=0: _answer({"prompt_tokens": 2000, "completion_tokens": 50,
                                                        "cost_in_usd_ticks": 30_000_000}))
    agent.call_xai("xai-k", "grok-4.20-0309-non-reasoning", "sys", "user", name="draft")
    monkeypatch.setattr(aicost.urllib.request, "urlopen", lambda req, timeout=0: _Resp(json.dumps(
        {"output": [], "usage": {"input_tokens": 9000, "output_tokens": 800,
                                 "server_side_tool_usage_details": {"web_search_calls": 20}}}).encode()))
    netfind._post("xai-k", {"model": "grok-4.20-0309-non-reasoning"}, 10)
    v = c.view()
    by = {x["what"]: x for x in v["by"]}
    assert by["Sidekick drafts"]["usd"] == pytest.approx(0.003) and not by["Sidekick drafts"]["est"]
    assert by["Find nets"]["est"] == 1 and by["Find nets"]["usd"] == pytest.approx(
        (9000 * 1.25 + 800 * 2.5) / 1e6 + 0.1, abs=1e-4)
    assert v["calls"] == 2 and v["month"] == pytest.approx(v["today"])
    assert v["recent"][0]["what"] == "Find nets" and v["recent"][0]["searches"] == 20
    assert v["buy_url"].endswith("/billing")


def test_the_budget_warns_then_pauses_the_background_features(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    st.costs                                          # the running station's ledger (start() makes it)
    c = st.costs
    c.settings({"budget_usd": 1})
    c.record("net_summary", "grok-4.20-0309-non-reasoning", {"cost_in_usd_ticks": int(0.85e10), "prompt_tokens": 1})
    w = c.warnings()
    assert [x["level"] for x in w] == ["warn"] and "$0.85 of its $1.00" in w[0]["say"]
    assert c.allow_background()
    c.record("chat", "grok-4.20-0309-non-reasoning", {"cost_in_usd_ticks": int(0.2e10), "prompt_tokens": 1})
    assert not c.allow_background() and c.warnings()[0]["level"] == "bad"
    monkeypatch.setattr(netsummary, "enabled", lambda s: True)
    asked = []
    st._summarizer = netsummary.Summarizer(st, call=lambda s, u: asked.append(1) or "x")
    assert st.summarizer.tick(NOW) == 0 and not asked                     # summaries wait for next month
    assert st.snapshot()["aicost"][0]["kind"] == "budget"
    c.settings({"budget_usd": 0})
    assert c.allow_background() and not c.warnings()


def test_out_of_credit_is_shown_until_a_call_works(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    st.costs                                          # the running station's ledger (start() makes it)

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                     io.BytesIO(b'{"error":"Your team has run out of credits"}'))
    monkeypatch.setattr(aicost.urllib.request, "urlopen", refuse)
    with pytest.raises(urllib.error.HTTPError) as e:
        agent.call_xai("xai-k", "grok-4.20-0309-non-reasoning", "s", "u")
    assert b"credits" in e.value.read()                                    # the caller can still read the body
    w = st.costs.warnings()
    assert w[0]["kind"] == "credit" and w[0]["buy"] == aicost.BUY_URL
    monkeypatch.setattr(aicost.urllib.request, "urlopen",
                        lambda req, timeout=0: _answer({"prompt_tokens": 10, "completion_tokens": 1}))
    agent.call_xai("xai-k", "grok-4.20-0309-non-reasoning", "s", "u")
    assert not st.costs.warnings()


def test_the_account_is_read_with_the_management_key_and_never_tops_up(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    st.costs                                          # the running station's ledger (start() makes it)
    monkeypatch.setenv("XAI_MANAGEMENT_KEY", "mgmt-key-0123456789abcdefghij")
    asked = []

    def opener(req, timeout=0):
        asked.append((req.get_method(), req.full_url))
        assert req.headers["Authorization"] == "Bearer mgmt-key-0123456789abcdefghij"
        if req.full_url.endswith("/validation"):
            return _Resp(b'{"scope":"SCOPE_TEAM","scopeId":"team-1","acls":[]}')
        if req.full_url.endswith("/prepaid/balance"):
            return _Resp(b'{"changes":[],"total":{"val":"-437"}}')
        if req.full_url.endswith("/usage"):
            q = json.loads(req.data)["analyticsRequest"]
            assert q["values"] == [{"name": "usd", "aggregation": "AGGREGATION_SUM"}]
            return _Resp(json.dumps({"timeSeries": [
                {"group": ["Chat grok-4.20"], "groupLabels": ["Chat grok-4.20"],
                 "dataPoints": [{"timestamp": "2026-11-01T00:00:00Z", "values": [1.25]}]},
                {"group": ["Speech"], "groupLabels": ["Speech"],
                 "dataPoints": [{"timestamp": "2026-11-01T00:00:00Z", "values": [0.5]}]}],
                "limitReached": False}).encode())
        raise AssertionError(req.full_url)
    st.costs.opener = opener
    a = st.costs.refresh()
    assert a["credit_usd"] == 4.37 and a["month_usd"] == 1.75 and a["by"][0]["what"] == "Chat grok-4.20"
    assert not any("top-up" in u for _, u in asked) and [m for m, _ in asked] == ["GET", "GET", "POST"]
    w = st.costs.warnings()
    assert w[0]["kind"] == "low" and "$4.37" in w[0]["say"]               # under the $5 line by default
    st.costs.settings({"low_credit_usd": 2})
    assert not st.costs.warnings()
    assert st.costs.view()["account"]["credit_usd"] == 4.37


def test_ai_costs_over_http(tmp_path, monkeypatch):
    st, _ = _station(tmp_path)
    st.costs                                          # the running station's ledger (start() makes it)
    st.costs.record("draft", "grok-4.20-0309-non-reasoning", {"cost_in_usd_ticks": 10_000_000, "prompt_tokens": 5})
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/api/aicost") as r:
            v = json.loads(r.read())
        assert v["calls"] == 1 and not v["mgmt_key"]
        assert _post(base, "/api/aicost", {"budget_usd": 25, "low_credit_usd": 3})["budget_usd"] == 25
        assert _post(base, "/api/aicost/refresh", {})["account"] == {}
        with urllib.request.urlopen(base + "/api/secrets") as r:
            assert "XAI_MANAGEMENT_KEY" in [k["name"] for k in json.loads(r.read())["keys"]]
        with pytest.raises(urllib.error.HTTPError):
            _post(base, "/api/aicost", {"budget_usd": -1})
    finally:
        srv.shutdown()
