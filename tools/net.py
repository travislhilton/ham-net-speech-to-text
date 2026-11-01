"""One-tap net control for the transcribe service: start, stop, status.

    python -m tools.net start "Springfield Valley ARES" --freq 146.94 --ncs W9XYZ
    python -m tools.net stop            # stops the one open net (asks for an id if several)
    python -m tools.net status          # open nets; --all for the recent list

Standard library only, so it runs on the home server, the dev laptop or the puck without installs.
URL: --url, else $EMCOMM_STT_URL, else http://localhost:8090.
Key: $EMCOMM_STT_KEY, else EMCOMM_STT_KEY= in ~/.config/emcomm-stt/env (never printed).
Times shown in local time; the API stores UTC."""
import argparse, json, os, sys, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path

ENV_FILE = Path.home() / ".config/emcomm-stt/env"


class CliError(Exception):
    pass


def api_key(env=os.environ, env_file=ENV_FILE) -> str | None:
    if env.get("EMCOMM_STT_KEY"):
        return env["EMCOMM_STT_KEY"]
    try:
        for line in Path(env_file).read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if line.startswith("EMCOMM_STT_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"") or None
    except OSError:
        pass
    return None


def http(method: str, url: str, key: str | None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise CliError(f"{method} {url} → HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise CliError(f"{method} {url} → {e.reason}") from None


def _local(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def describe(n: dict) -> str:
    freq = f" {n['frequency_mhz']} MHz" if n.get("frequency_mhz") else ""
    ncs = f" NCS {n['net_control']}" if n.get("net_control") else ""
    end = "open" if not n.get("ended_utc") else f"ended {_local(n['ended_utc'])}"
    return f"#{n['id']} {n['name']}{freq}{ncs} — started {_local(n['started_utc'])}, {end}"


def run(argv, call, out=print) -> int:
    """`call(method, path, body)` does the HTTP; injectable for tests."""
    p = argparse.ArgumentParser(prog="python -m tools.net", description=__doc__.split("\n")[0])
    p.add_argument("--url", default=os.environ.get("EMCOMM_STT_URL", "http://localhost:8090"))
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="open a net now (or at --at)")
    s.add_argument("name")
    s.add_argument("--freq", type=float, help="MHz, e.g. 146.94 — splits overlapping nets")
    s.add_argument("--ncs", help="net control callsign")
    s.add_argument("--notes")
    s.add_argument("--at", help="ISO start time, to log a net after the fact")
    t = sub.add_parser("stop", help="close a net now (or at --at)")
    t.add_argument("net_id", nargs="?", type=int, help="default: the one open net")
    t.add_argument("--at", help="ISO end time")
    st = sub.add_parser("status", help="list open nets")
    st.add_argument("--all", action="store_true", help="recent nets, open or not")
    a = p.parse_args(argv)

    if a.cmd == "start":
        open_nets = call("GET", "/nets?open=true", None)
        body = {"name": a.name}
        for k, v in (("frequency_mhz", a.freq), ("net_control", a.ncs), ("notes", a.notes), ("started_utc", a.at)):
            if v is not None:
                body[k] = v
        n = call("POST", "/nets/start", body)
        out(f"started {describe(n)}")
        if open_nets:
            out("note: also open — " + "; ".join(describe(o) for o in open_nets))
        out(f"stop it with: python -m tools.net stop {n['id']}")
        return 0

    if a.cmd == "stop":
        net_id = a.net_id
        if net_id is None:
            open_nets = call("GET", "/nets?open=true", None)
            if not open_nets:
                raise CliError("no open net to stop")
            if len(open_nets) > 1:
                raise CliError("several nets are open; name one:\n  " + "\n  ".join(describe(o) for o in open_nets))
            net_id = open_nets[0]["id"]
        n = call("POST", f"/nets/{net_id}/stop", {"ended_utc": a.at} if a.at else {})
        out(f"stopped {describe(n)}; back-filled {n.get('backfilled', 0)} clip(s)")
        out(f"report: python -m analyze.net_report --net-id {n['id']}")
        return 0

    nets = call("GET", "/nets" if a.all else "/nets?open=true", None)
    if not nets:
        out("no open nets" if not a.all else "no nets")
    for n in nets:
        out(describe(n))
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    url = os.environ.get("EMCOMM_STT_URL", "http://localhost:8090")
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
    key = api_key()
    try:
        return run(argv, lambda m, path, body: http(m, url.rstrip("/") + path, key, body))
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
