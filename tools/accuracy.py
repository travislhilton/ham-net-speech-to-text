"""How accurate the station is, measured against the operator's corrections (tap-to-correct on the dashboard).

    python -m tools.accuracy                     # ask the running station (http://localhost:8091)
    python -m tools.accuracy --url http://192.168.1.20:8091
    python -m tools.accuracy --data data         # read the station's data folder directly (station stopped is fine)
    python -m tools.accuracy --json

Word error rate of the live text, the accurate pass and (when there is one) the consensus text against every
corrected transmission, overall and per radio, mode and net (station/corrections.py)."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def from_station(url: str) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + "/api/accuracy", timeout=15) as r:
        return json.loads(r.read())


def from_data(data: Path, me: str | None = None) -> dict:
    from station import corrections
    from station.history import History
    h = History(Path(data))
    try:
        h.catch_up(Path(data) / "logs")
        return corrections.accuracy(h.corrected_rows(), me)
    finally:
        h.close()


def main(argv=None) -> int:
    from station import corrections
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="http://localhost:8091", help="the station's dashboard address")
    ap.add_argument("--data", help="read this data folder instead of asking the station")
    ap.add_argument("--call", help="your callsign (with --data: compared letter by letter like the station does)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        acc = from_data(Path(a.data), a.call) if a.data else from_station(a.url)
    except (urllib.error.URLError, OSError) as e:
        print(f"Could not reach the station at {a.url}: {e}. Use --data <folder> to read its data directly.",
              file=sys.stderr)
        return 2
    print(json.dumps(acc, indent=2) if a.json else corrections.table(acc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
