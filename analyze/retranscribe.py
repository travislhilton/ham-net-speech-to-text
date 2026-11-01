"""Re-run stored WAVs through the current engine (model + vocabulary) and update their rows.

The WAV is the record; a transcript is derived data. Re-run when the roster grows, the model
changes, or a row came from somewhere other than the home server.

  python -m analyze.retranscribe --stale            # rows whose model or host differs from now
  python -m analyze.retranscribe --net-id 3         # every row of one net
  python -m analyze.retranscribe --since 2026-11-01 # every row started on/after a date (UTC)
  python -m analyze.retranscribe --id 17 --id 18
Add --dry-run to list what would change without writing. Clears `structured` on rows whose text
changed, so the next net_report re-structures them."""
import argparse, json
from pathlib import Path


def select(s, Transmission, a, model, host):
    q = s.query(Transmission)
    if a.id:
        q = q.filter(Transmission.id.in_(a.id))
    if a.net_id is not None:
        q = q.filter(Transmission.net_id == a.net_id)
    if a.since:
        q = q.filter(Transmission.started_utc >= a.since)
    if a.stale:
        q = q.filter((Transmission.model.is_distinct_from(model)) | (Transmission.host.is_distinct_from(host)))
    return q.order_by(Transmission.started_utc)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--id", type=int, action="append")
    ap.add_argument("--net-id", type=int)
    ap.add_argument("--since")
    ap.add_argument("--stale", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not (a.id or a.net_id is not None or a.since or a.stale):
        ap.error("pick rows: --id, --net-id, --since or --stale")

    from db.models import Session, Transmission
    from transcribe.engine import HOST, Engine
    eng = Engine()
    changed = missing = same = 0
    with Session() as s:
        rows = select(s, Transmission, a, eng.model, HOST).all()
        print(f"{len(rows)} row(s) selected; model {eng.model}, roster {len(eng.roster)}")
        for t in rows:
            wav = Path(t.wav_path)
            if not wav.exists():
                print(f"  #{t.id}: WAV missing at {wav} — skipped"); missing += 1; continue
            out = eng.transcribe(wav)
            if out["text"] == (t.text or "") and t.model == out["model"] and t.host == out["host"]:
                same += 1; continue
            print(f"  #{t.id} [{t.model}@{t.host}]\n    was: {t.text!r}\n    now: {out['text']!r}")
            changed += 1
            if a.dry_run:
                continue
            if out["text"] != (t.text or ""):
                t.structured = None
            t.text, t.callsigns, t.segments = out["text"], out["callsigns"], out["segments"]
            t.model, t.host = out["model"], out["host"]
            out.update(id=t.id, net_id=t.net_id)
            wav.with_suffix(".transcript.json").write_text(json.dumps(out, indent=2))
        if not a.dry_run:
            s.commit()
    print(f"changed {changed}, unchanged {same}, missing WAV {missing}" + (" (dry run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
