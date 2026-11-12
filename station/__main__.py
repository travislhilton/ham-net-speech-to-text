"""python -m station [--simulate WAV] [--radio ID] [--engine fake] [--port N] [--no-listen]

Starts the live station: every configured radio, the transcription engines from config/engine.yaml,
and the dashboard (default http://localhost:8091/)."""
import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

import soundfile as sf

from .core import ROOT, Station, load_config
from .server import serve


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m station")
    ap.add_argument("--config", default=str(ROOT / "config" / "station.yaml"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--engine", choices=["auto", "fake"], default="auto")
    ap.add_argument("--simulate", help="WAV file(s) to play in as transmissions, comma separated")
    ap.add_argument("--radio", help="radio id for --simulate (default: the first radio)")
    ap.add_argument("--no-listen", action="store_true", help="do not open the radios' audio inputs")
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    ap.add_argument("--log", help="append output to this file (used when started at logon without a console)")
    a = ap.parse_args(argv)
    if a.log or sys.stdout is None:                       # pythonw has no console: sys.stdout is None
        log = Path(a.log or ROOT / "data" / "station.log"); log.parent.mkdir(parents=True, exist_ok=True)
        from .retention import trim_log                  # over 10 MB: keep its last 2 MB
        trim_log(log)
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
        import datetime as _dt; print(f"--- start {_dt.datetime.now().isoformat(timespec='seconds')}")

    cfg = load_config(Path(a.config))
    from . import radios as radmod                       # Radios… on the dashboard: data/radios.json wins once saved
    cfg["radios"] = radmod.load(ROOT / "data" / "radios.json", cfg["radios"])
    for r in cfg["radios"]:
        r.setdefault("gate", "carrier")
    if a.simulate and not cfg["radios"]:
        cfg["radios"] = [{"id": "sim", "name": "Simulated radio", "device": None, "gate": "carrier", "freq_mhz": 146.94}]
    from . import engine
    if a.engine == "fake":
        final, partial = (lambda: engine.FakeEngine(name="fake-final")), (lambda: engine.FakeEngine(name="fake-partial"))
    else:
        choice = engine.load_choice()
        if not choice.get("final"):
            sys.exit("config/engine.yaml has no engine; run tools/pick_engine.py (the installer does)")
        final = lambda: engine.make(choice["final"])
        partial = (lambda: engine.make(choice["partial"])) if choice.get("partial") else None

    from .callbook import Callbook
    qrz = None
    try:
        from hamkit import qrz as qrzmod
        if qrzmod.credentials():
            qrz = qrzmod.Client()
    except Exception as e:  # noqa: BLE001
        print("QRZ unavailable:", e)
    st = Station(cfg, final, partial, callbook=Callbook(qrz=qrz))
    if a.engine != "fake":
        st.accurate_spec = choice.get("accurate")         # the accurate pass on this PC (tools/pick_accurate.py)
        if (choice["final"].get("device") or "CPU").upper() != "CPU":   # a stuck graphics engine: the same model on the CPU
            st._cpu_factory = lambda: engine.make({**choice["final"], "device": "CPU"})
    st.start(listen=not (a.no_listen or a.simulate))
    port = a.port or cfg["port"]
    srv = serve(st, cfg["bind"], port)
    print(f"station {st.me or '(no callsign)'}: {len(cfg['radios'])} radio(s); dashboard http://localhost:{port}/",
          flush=True)
    if a.simulate:
        rid = a.radio or cfg["radios"][0]["id"]

        def play():
            for f in a.simulate.split(","):
                audio, sr = sf.read(f, dtype="float32")
                if audio.ndim > 1:
                    audio = audio.mean(axis=1)
                st.inject(rid, audio, realtime=True)
        threading.Thread(target=play, daemon=True).start()
    if a.open:
        webbrowser.open(f"http://localhost:{port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        st.stop.set()
        sys.stdout.flush()
        if os.name == "nt":          # OpenVINO GPU teardown can hang; nothing is left to save
            import ctypes
            k = ctypes.windll.kernel32; k.TerminateProcess(k.GetCurrentProcess(), 0)
        os._exit(0)


if __name__ == "__main__":
    main()
