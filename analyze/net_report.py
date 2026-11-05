"""Build an ICS-309 communications log + narrative summary for one net.

With the LLM server reachable: per-transmission structuring (stored) + an LLM summary. Without it:
a rules-only log and roster (nothing stored), so a net is never left with no record."""
import argparse, csv, io, json, os, sys
from . import rules
from .llm import chat, HEAVY, LLMUnavailable
from .structure import structure

SUMMARY = """You are an EMCOMM net logger. From the ordered transmissions (JSON), write:
1) a check-in roster (callsign, tactical, first/last heard),
2) formal traffic list with precedence and ICS-213 message text where present,
3) a concise after-action summary (3-6 sentences): participation, traffic handled, gaps/problems heard.
Plain markdown. Do not invent callsigns not present."""


def ics309_csv(rows):
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Time (UTC)", "From", "To", "Message / Subject", "Method", "Operator"])
    for r in rows:
        s = r.get("structured") or {}
        w.writerow([r["started_utc"], s.get("from_callsign"), s.get("to_callsign") or "NET",
                    (s.get("traffic") or {}).get("summary") or (r["text"] or "")[:120], "Voice", r.get("station")])
    return buf.getvalue()


def build(txs, structure_fn=structure, chat_fn=chat):
    """Pure-ish core: returns (rows, summary_md, degraded_reason). Stores LLM structure on `txs`."""
    rows, ctx, reason = [], [], None
    for t in txs:
        st = t.structured
        if not st and reason is None:
            try:
                st = t.structured = structure_fn(t.text or "", t.callsigns or [], "\n".join(ctx[-5:]))
            except LLMUnavailable as e:
                reason = str(e)
        if not st:
            st = rules.structure(t.text or "", t.callsigns or [])
        ctx.append(f"[{t.started_utc:%H:%M:%S}] {t.text}")
        rows.append({"started_utc": t.started_utc.isoformat(), "text": t.text, "station": t.station,
                     "callsigns": t.callsigns or [], "structured": st})
    if reason is None:
        try:
            return rows, chat_fn(HEAVY, SUMMARY, json.dumps(rows)[:120_000], max_tokens=2500), None
        except LLMUnavailable as e:
            reason = str(e)
    return rows, None, reason


def run(net_id, out_dir):
    from db.models import Session, Transmission
    with Session() as s:
        txs = s.query(Transmission).filter_by(net_id=net_id).order_by(Transmission.started_utc).all()
        rows, md, reason = build(txs)
        s.commit()   # LLM structure only; rules output lives in `rows`, never on the row
    if reason:
        md = rules.summary_md(net_id, rows, reason)
        print(f"warning: LLM unavailable — rules-only report ({reason})", file=sys.stderr)
    os.makedirs(out_dir, exist_ok=True)
    open(f"{out_dir}/net-{net_id}-ICS309.csv", "w", encoding="utf-8").write(ics309_csv(rows))
    open(f"{out_dir}/net-{net_id}-summary.md", "w", encoding="utf-8").write(md)
    print(md)
    return 2 if reason else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--net-id", type=int, required=True)
    ap.add_argument("--out", default="reports"); a = ap.parse_args(); sys.exit(run(a.net_id, a.out))
