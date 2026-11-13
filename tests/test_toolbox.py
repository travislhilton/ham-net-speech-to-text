"""The thinking model's tools (station/toolbox.py, agent.call_xai_tools): library search and path analysis on demand."""
import json

from station import agent as A
from station.toolbox import Toolbox
from tests.test_agent import _station


def _tc(i, name, args):
    return {"id": i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _reply(content="", calls=None):
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = calls
    return {"choices": [{"message": m}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class FakeLib:
    def context_for(self, q, n):
        return f"[manual] about {q}" if "net" in q else ""


def _box(tmp_path):
    st = _station(tmp_path)
    st.agent._lib = FakeLib()
    st.agent.topics.sites = [{"name": "Stake EOC", "lat": 41.5, "lon": -112.0, "height_m": 10},
                             {"name": "Shelter A", "lat": 41.6, "lon": -112.1, "height_m": 5}]
    return st, Toolbox(st.agent, "ic")


def test_tools_return_bounded_text_and_failures_as_text(tmp_path):
    st, tb = _box(tmp_path)
    assert "about net hours" in tb.run("search_library", '{"query": "net hours"}')
    assert "nothing in the library" in tb.run("search_library", '{"query": "zzz"}')
    assert "Stake EOC" in tb.run("list_sites", "{}")
    assert "unknown site" in tb.run("path_analysis", '{"site_a": "Stake EOC", "site_b": "Nowhere"}')
    assert "unknown tool" in tb.run("launch_missiles", "{}")
    assert "tool failed" in tb.run("search_library", "not json")


def test_path_tool_uses_the_named_sites(tmp_path, monkeypatch):
    st, tb = _box(tmp_path)
    import station.topics as T
    seen = {}
    monkeypatch.setattr(T, "path_facts", lambda sites, mhz=146.0, **k: seen.update(n=[s["name"] for s in sites], f=mhz) or ["ok"])
    assert tb.run("path_analysis", {"site_a": "stake eoc", "site_b": "Shelter A", "mhz": 446.0}) == "ok"
    assert seen == {"n": ["Stake EOC", "Shelter A"], "f": 446.0}


def test_call_xai_tools_runs_the_tools_then_asks_for_the_schema(tmp_path):
    st, tb = _box(tmp_path)
    posts = []
    script = [_reply(calls=[_tc("c1", "search_library", {"query": "net hours"})]),
              _reply("done thinking"),
              _reply(json.dumps({"answer": "The net is at 7:30.", "confident": True, "why": "manual"}))]

    def post(key, body, timeout):
        posts.append(body)
        return script[len(posts) - 1]
    out, usage = A.call_xai_tools("k", "grok-4.5", "sys", "user", tb, schema=A.THINK_SCHEMA, name="answer", post=post)
    assert out["answer"] == "The net is at 7:30." and usage["tools"] == ["search_library"]
    assert "tools" in posts[0] and "response_format" not in posts[0]
    tool_msg = [m for m in posts[1]["messages"] if m["role"] == "tool"][0]
    assert "about net hours" in tool_msg["content"] and tool_msg["tool_call_id"] == "c1"
    assert "tools" not in posts[-1] and "response_format" in posts[-1]     # the final answer is in the schema


def test_rounds_are_capped(tmp_path):
    st, tb = _box(tmp_path)
    n = []

    def post(key, body, timeout):
        n.append(1)
        if "response_format" in body:
            return _reply(json.dumps({"answer": "x", "confident": False, "why": "y"}))
        return _reply(calls=[_tc("c", "list_sites", {})])
    A.call_xai_tools("k", "m", "s", "u", tb, schema=A.THINK_SCHEMA, rounds=3, post=post)
    assert len(n) == 4                                   # 3 tool rounds + the answer
