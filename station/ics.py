"""Turn a net or conversation (a span of transmissions on one radio) into ICS form values for Winlink:
ICS-309 Communications Log (always; 30 rows a page, so long nets make several pages) and an ICS-213
General Message summarising it. The operator reviews and edits everything before it reaches the outbox."""
from datetime import datetime, timedelta

from . import speaker

ROWS_309 = 30
CALL_MAX, SUB_MAX = 13, 90          # Winlink's Form-309 viewer: From/To maxlength 13, Subject 90, Page 2


def fit(s: str, n: int) -> str:
    """Shorten to the form's box, at a word boundary, marked with '...'."""
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n - 3].rsplit(" ", 1)[0] if " " in s[:n - 3] else s[:n - 3]
    return cut.rstrip(" ,;:") + "..."


def sessions(tx: list[dict], rid: str, gap_min: int = 15) -> list[dict]:
    """The nets and conversations heard on `rid`, oldest first: a run of transmissions on one frequency with no
    quiet gap longer than `gap_min` minutes; a scheduled net's first transmission starts a new one (the station
    tags each transmission with its net: core.net_of), and so does the first one after a net was closed (`after`:
    station/netclose.py). The dashboard groups its log the same way (static/index.html,
    groupSessions) and asks for Winlink messages by a session's first and last transmission times."""
    rows = sorted((t for t in tx if t.get("radio") == rid and t.get("status") == "final"), key=lambda t: t["start"])
    out = []
    for t in rows:
        at = datetime.fromisoformat(t["start"])
        cur = out[-1] if out else None
        net = (t.get("net") or {}).get("key")
        closed = bool(cur and t.get("after") and t["after"] in (cur["net"], f"{rid}@{cur['start']}"))
        if (cur and not closed and at - datetime.fromisoformat(cur["end"]) <= timedelta(minutes=gap_min)
                and t.get("freq_mhz") == cur["freq_mhz"] and (net == cur["net"] or not net)):
            cur["end"] = t["start"]; cur["count"] += 1
        else:
            out.append({"start": t["start"], "end": t["start"], "freq_mhz": t.get("freq_mhz"), "count": 1, "net": net})
    return out


def session_bounds(tx: list[dict], rid: str, gap_min: int = 15) -> tuple[str, str] | None:
    """Default span: the latest net or conversation on `rid`."""
    s = sessions(tx, rid, gap_min)
    return (s[-1]["start"], s[-1]["end"]) if s else None


def view(t: dict, agreed: bool = True) -> dict:
    """A transmission as the forms see it: the operator's correction when there is one (tap-to-correct), else the
    accurate pass's text and callsigns, else the live text; and the best speaker (station/speaker.py best:
    operator-set > corrected text > stated > net control > voice). `agreed`: with the net-wide callsign agreement
    and local-name fix-ups applied (station/consensus.py; the heard text kept as heard_text) — never over the
    operator's correction."""
    a = t.get("corrected") or t.get("accurate")
    v = {**t, **{k: a[k] for k in ("text", "callsigns", "noise") if k in a}} if a else dict(t)
    v["speaker"], v["speaker_how"] = speaker.best(t)
    if agreed and t.get("consensus") and not t.get("corrected"):      # the operator's correction always wins
        from .consensus import apply_view
        v = apply_view(v, t["consensus"])
    return v


def select(tx, rid, start, end, include_noise=False):
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    out = [view(t) for t in tx if t.get("radio") == rid and t.get("status") == "final"
           and s <= datetime.fromisoformat(t["start"]) <= e]
    return out if include_noise else [t for t in out if not t.get("noise")]


def participants(span, me_calls: set[str], heard: dict) -> list[dict]:
    seen = []
    said = lambda t: list(t.get("callsigns", [])) + ([t["speaker"]] if t.get("speaker") and t["speaker"] not in t.get("callsigns", []) else [])
    for t in span:
        for c in said(t):                                # an operator-set speaker takes part even if never named
            if c not in seen and c.split("-")[0] not in me_calls:
                seen.append(c)
    out = []
    for c in seen:
        card = (heard.get(c) or {}).get("card") or {}
        lapsed = card.get("license_valid") is False                     # expired / cancelled / terminated
        out.append({"call": c, "name": card.get("name"), "confirmed": bool(card.get("sources")) and not lapsed,
                    "lapsed": card.get("license_status") if lapsed else None,
                    "count": sum(c in said(t) for t in span)})
    return out


def drafts(span, radio: dict, op: dict, people: list[dict], net_label: str | None, title: str | None,
           minutes: str = "", local_terms: str = "", summary: str = "") -> list[dict]:
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
                     "from": fit(t.get("speaker") or "?", CALL_MAX),
                     "to": fit(others[0] if len(others) == 1 else "NET", CALL_MAX),
                     "sub": fit(t.get("text") or "", SUB_MAX)})
    out = []
    pages = [rows[i:i + ROWS_309] for i in range(0, len(rows), ROWS_309)]
    for n, page in enumerate(pages, 1):
        v = {"Title": title, "Task": "", "TaskName": freq, "OpPer": "1", "Page": str(n),
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
           f"{len(span)} transmission{'' if len(span) == 1 else 's'} logged by {sender}{' (' + net_label + ')' if net_label else ''}.\n\n"
           + (f"Summary:\n{summary}\n\n" if summary else "")         # the minutes' Summary (owner 2026-11-14)
           + f"Stations heard:\n{who}\n")
    if traffic:
        msg += "\nTraffic / relays / emergency calls heard:\n" + "\n".join(
            f"  {datetime.fromisoformat(t['start']):%H:%M} {t.get('speaker') or '?'}: {t.get('text', '')[:160]}" for t in traffic)
    if minutes:
        msg += "\n\nMinutes (drafted from the transcript, reviewed by the operator):\n" + minutes
    if local_terms:                       # the area's names said on this net, for a reader outside the area
        msg += "\n\n" + local_terms
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
