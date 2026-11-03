"""Turn a net or conversation (a span of transmissions on one radio) into ICS form values for Winlink:
ICS-309 Communications Log (always; 30 rows a page, so long nets make several pages) and an ICS-213
General Message summarising it. The operator reviews and edits everything before it reaches the outbox."""
from datetime import datetime, timedelta

ROWS_309 = 30


def sessions(tx: list[dict], rid: str, gap_min: int = 15) -> list[dict]:
    """The nets and conversations heard on `rid`, oldest first: a run of transmissions on one frequency with no
    quiet gap longer than `gap_min` minutes. The dashboard groups its log the same way (static/index.html,
    groupSessions) and asks for Winlink messages by a session's first and last transmission times."""
    rows = sorted((t for t in tx if t.get("radio") == rid and t.get("status") == "final"), key=lambda t: t["start"])
    out = []
    for t in rows:
        at = datetime.fromisoformat(t["start"])
        cur = out[-1] if out else None
        if (cur and at - datetime.fromisoformat(cur["end"]) <= timedelta(minutes=gap_min)
                and t.get("freq_mhz") == cur["freq_mhz"]):
            cur["end"] = t["start"]; cur["count"] += 1
        else:
            out.append({"start": t["start"], "end": t["start"], "freq_mhz": t.get("freq_mhz"), "count": 1})
    return out


def session_bounds(tx: list[dict], rid: str, gap_min: int = 15) -> tuple[str, str] | None:
    """Default span: the latest net or conversation on `rid`."""
    s = sessions(tx, rid, gap_min)
    return (s[-1]["start"], s[-1]["end"]) if s else None


def select(tx, rid, start, end):
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return [t for t in tx if t.get("radio") == rid and t.get("status") == "final"
            and s <= datetime.fromisoformat(t["start"]) <= e]


def participants(span, me_calls: set[str], heard: dict) -> list[dict]:
    seen = []
    for t in span:
        for c in t.get("callsigns", []):
            if c not in seen and c.split("-")[0] not in me_calls:
                seen.append(c)
    out = []
    for c in seen:
        card = (heard.get(c) or {}).get("card") or {}
        out.append({"call": c, "name": card.get("name"), "confirmed": bool(card.get("sources")),
                    "count": sum(c in t.get("callsigns", []) for t in span)})
    return out


def drafts(span, radio: dict, op: dict, people: list[dict], net_label: str | None, title: str | None) -> list[dict]:
    if not span:
        return []
    first, last = datetime.fromisoformat(span[0]["start"]), datetime.fromisoformat(span[-1]["start"])
    f_mhz = span[0].get("freq_mhz") or radio.get("freq_mhz")          # the net's own frequency, not the radio's now
    mode = radio.get("mode", "") if f_mhz == radio.get("freq_mhz") else ""
    freq = f"{f_mhz:.3f} MHz {mode}".strip() if f_mhz else ""
    title = title or f"Net on {freq}" if freq else (title or "Radio net")
    name = op.get("name") or op.get("callsign") or ""
    sender = op.get("winlink_account") or op.get("callsign") or ""
    rows = []
    for t in span:
        others = [c for c in t.get("callsigns", []) if c != t.get("speaker")]
        rows.append({"time": datetime.fromisoformat(t["start"]).strftime("%Y-%m-%d %H:%M"),
                     "from": t.get("speaker") or "(unidentified)", "to": ", ".join(others) or "NET",
                     "sub": (t.get("text") or "")[:200]})
    out = []
    pages = [rows[i:i + ROWS_309] for i in range(0, len(rows), ROWS_309)]
    for n, page in enumerate(pages, 1):
        v = {"Title": title, "Task": "", "TaskName": freq, "OpPer": "1", "Page": str(n) if len(pages) == 1 else f"{n} of {len(pages)}",
             "ActivityDateTime1": first.strftime("%Y-%m-%d %H:%M"), "OpName": name, "OperId": sender,
             "MsgSender": sender, "name": name}
        for i in range(1, ROWS_309 + 1):
            r = page[i - 1] if i <= len(page) else {"time": "", "from": "", "to": "", "sub": ""}
            v |= {f"Time{i}": r["time"], f"From{i}": r["from"], f"To{i}": r["to"], f"Sub{i}": r["sub"]}
        out.append({"form": "ics309", "label": f"ICS-309 Communications Log" + (f", page {n} of {len(pages)}" if len(pages) > 1 else ""),
                    "values": v, "rows": page})
    who = "\n".join(f"  {p['call']}{' — ' + p['name'] if p.get('name') else ''} ({p['count']} transmission{'s' if p['count'] != 1 else ''})"
                    for p in people) or "  (no callsigns identified)"
    traffic = [t for t in span if set(t.get("hint_keys") or []) & {"emergency", "traffic", "relay"}]
    msg = (f"{title}\n{first:%Y-%m-%d %H:%M} to {last:%H:%M} local{', ' + freq if freq else ''}.\n"
           f"{len(span)} transmissions logged by {sender}{' (' + net_label + ')' if net_label else ''}.\n\n"
           f"Stations heard:\n{who}\n")
    if traffic:
        msg += "\nTraffic / relays / emergency calls heard:\n" + "\n".join(
            f"  {datetime.fromisoformat(t['start']):%H:%M} {t.get('speaker') or '?'}: {t.get('text', '')[:160]}" for t in traffic)
    msg += "\n\nThe full log is in the attached ICS-309. Machine transcription, reviewed by the operator before sending."
    now = datetime.now()
    out.append({"form": "ics213", "label": "ICS-213 General Message (summary)",
                "values": {"inc_name": title, "To_Name": "Net participants", "fm_name": f"{name}, {op.get('callsign', '')}".strip(", "),
                           "Subjectline": f"{title} summary", "Mdate": now.strftime("%Y-%m-%d"), "mtime": now.strftime("%H:%M"),
                           "Message": msg, "Approved_Name": name, "Approved_PosTitle": "Radio Operator",
                           "IsExercise": "** THIS IS AN EXERCISE **" if net_label == "exercise" else "",
                           "FormTitle": "", "txtStr": "", "theMsgSender": sender, "MsgSender": sender, "name": name,
                           "Templateversion": "", "mapLat": "", "mapLon": "", "MGRS": "", "locationSource": ""}})
    return out
