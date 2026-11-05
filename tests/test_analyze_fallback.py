"""analyze/ degrades to a rules-only record when the LLM server is unreachable or no key is set."""
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from analyze import llm, net_report, rules


def _tx(sec, text, callsigns, structured=None):
    return NS(started_utc=datetime(2026, 11, 2, 3, 0, sec, tzinfo=timezone.utc), text=text,
              callsigns=callsigns, station="W9XYZ", structured=structured)


def test_rules_speaker_is_last_callsign_after_this_is():
    s = rules.structure("W9XYZ this is K7ABC checking in, no traffic", ["W9XYZ", "K7ABC"])
    assert (s["from_callsign"], s["to_callsign"], s["kind"], s["source"]) == ("K7ABC", "W9XYZ", "check_in", "rules")
    assert rules.structure("K7ABC checking out", ["K7ABC"])["kind"] == "check_out"
    assert rules.structure("static", [])["from_callsign"] is None


def test_roster_first_last_count():
    rows = [{"started_utc": "a", "callsigns": ["K7ABC"]}, {"started_utc": "b", "callsigns": ["W9XYZ", "K7ABC"]}]
    assert rules.roster(rows) == [{"callsign": "K7ABC", "first_utc": "a", "last_utc": "b", "count": 2},
                                  {"callsign": "W9XYZ", "first_utc": "b", "last_utc": "b", "count": 1}]


def test_chat_without_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("LITELLM_KEY", raising=False)
    with pytest.raises(llm.LLMUnavailable, match="LITELLM_KEY"):
        llm.chat(llm.LIGHT, "s", "u")


def test_chat_to_a_dead_gateway_fails_fast(monkeypatch):
    pytest.importorskip("openai", reason="analyze/ runs on the home server (analyze/requirements.txt)")
    monkeypatch.setenv("LITELLM_KEY", "x"); monkeypatch.setenv("LITELLM_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(llm, "TIMEOUT_S", 3.0); monkeypatch.setattr(llm, "_client", None)
    with pytest.raises(llm.LLMUnavailable):
        llm.chat(llm.LIGHT, "s", "u")
    llm._client = None


def test_build_degrades_and_never_stores_rules_output():
    txs = [_tx(0, "net control this is W9XYZ", ["W9XYZ"]), _tx(5, "W9XYZ this is K7ABC checking in", ["W9XYZ", "K7ABC"])]
    calls = []

    def down(*a):
        calls.append(a); raise llm.LLMUnavailable("gateway down")
    rows, md, reason = net_report.build(txs, structure_fn=down, chat_fn=down)
    assert md is None and reason == "gateway down"
    assert len(calls) == 1                        # gave up after the first failure, no per-row timeouts
    assert all(t.structured is None for t in txs)  # rules output stays out of the DB
    assert rows[1]["structured"]["from_callsign"] == "K7ABC"
    assert "K7ABC" in net_report.ics309_csv(rows)
    assert "rules-only" in rules.summary_md(7, rows, reason) and "| K7ABC |" in rules.summary_md(7, rows, reason)


def test_build_with_llm_stores_structure_and_summarises():
    txs = [_tx(0, "hello", ["K7ABC"]), _tx(1, "again", ["K7ABC"], structured={"from_callsign": "K7ABC"})]
    rows, md, reason = net_report.build(txs, structure_fn=lambda *a: {"from_callsign": "K7ABC", "kind": "check_in"},
                                        chat_fn=lambda *a, **k: "# summary")
    assert (md, reason) == ("# summary", None)
    assert txs[0].structured["kind"] == "check_in"
