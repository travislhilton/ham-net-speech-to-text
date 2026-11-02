"""Rules-only analysis: what a net log can say without an LLM. Used when the LLM server is unreachable.

Ham convention is "<called> this is <calling>", so with "this is"/"de" the LAST callsign spoke.
Rules output carries "source": "rules" and is never written to the database — the LLM pass
fills `structured` later."""
import re

_SPEAKER = re.compile(r"\b(this is|de)\b", re.I)


def structure(text: str, callsigns: list[str]) -> dict:
    cs = [c for c in (callsigns or []) if c]
    t = (text or "").lower()
    if cs and _SPEAKER.search(t) and len(cs) >= 2:
        frm, to = cs[-1], cs[0]
    else:
        frm, to = (cs[0] if cs else None), None
    if "checking out" in t or "check out" in t:
        kind = "check_out"
    elif "checking in" in t or "check in" in t or "check-in" in t:
        kind = "check_in"
    elif "net control" in t and not cs[1:]:
        kind = "net_control"
    else:
        kind = "unknown"
    return {"from_callsign": frm, "to_callsign": to, "tactical": None, "kind": kind, "precedence": None,
            "traffic": {"summary": None, "location": None, "needs": [], "ics213_message": None},
            "confidence": 0.3, "source": "rules"}


def roster(rows: list[dict]) -> list[dict]:
    """Every callsign heard, first/last time and transmission count, in first-heard order."""
    seen: dict[str, dict] = {}
    for r in rows:
        for c in r.get("callsigns") or []:
            e = seen.setdefault(c, {"callsign": c, "first_utc": r["started_utc"], "count": 0})
            e["last_utc"] = r["started_utc"]; e["count"] += 1
    return list(seen.values())


def summary_md(net_id: int, rows: list[dict], reason: str) -> str:
    lines = [f"# Net {net_id} — rules-only report", "",
             f"_The LLM summary was not produced: {reason}. Re-run `python -m analyze.net_report "
             f"--net-id {net_id}` once the LLM server aliases are live._", "",
             f"Transmissions: {len(rows)}", "", "| Callsign | First heard (UTC) | Last heard (UTC) | Transmissions |",
             "|---|---|---|---|"]
    lines += [f"| {e['callsign']} | {e['first_utc']} | {e['last_utc']} | {e['count']} |" for e in roster(rows)]
    return "\n".join(lines) + "\n"
