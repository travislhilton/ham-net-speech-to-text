"""Build an ICS-309 communications log + narrative summary for one net."""
import argparse, csv, io, json, os
from .llm import chat, HEAVY
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
                    (s.get("traffic") or {}).get("summary") or r["text"][:120], "Voice", r.get("station")])
    return buf.getvalue()


def run(net_id, out_dir):
    from db.models import Session, Transmission
    with Session() as s:
        txs = s.query(Transmission).filter_by(net_id=net_id).order_by(Transmission.started_utc).all()
        rows, ctx = [], []
        for t in txs:
            if not t.structured:
                t.structured = structure(t.text or "", t.callsigns or [], "\n".join(ctx[-5:]))
            ctx.append(f"[{t.started_utc:%H:%M:%S}] {t.text}")
            rows.append({"started_utc": t.started_utc.isoformat(), "text": t.text, "station": t.station,
                         "structured": t.structured})
        s.commit()
    os.makedirs(out_dir, exist_ok=True)
    open(f"{out_dir}/net-{net_id}-ICS309.csv", "w").write(ics309_csv(rows))
    md = chat(HEAVY, SUMMARY, json.dumps(rows)[:120_000], max_tokens=2500)
    open(f"{out_dir}/net-{net_id}-summary.md", "w").write(md)
    print(md)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--net-id", type=int, required=True)
    ap.add_argument("--out", default="reports"); a = ap.parse_args(); run(a.net_id, a.out)
