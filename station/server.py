"""Dashboard server (standard library only): the page, a Server-Sent Events stream, state, lookups, audio.
Reach it from the station PC (http://localhost:8091/) or any PC on the LAN / Tailscale (the installer's
firewall rule allows only those)."""
import json
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .schedule import NoSuchNet
from . import speaker

STATIC = Path(__file__).parent / "static"


def with_who(kind: str, data):
    """Each transmission leaves with `who` = {call, how}: the best speaker (station/speaker.py best), so the page
    shows exactly what the ICS-309 will say."""
    def add(t):
        call, how = speaker.best(t)
        return {**t, "who": {"call": call, "how": how} if call else None}
    if kind == "tx" and isinstance(data, dict):
        return add(data)
    if kind == "snapshot" and isinstance(data, dict) and isinstance(data.get("tx"), list):
        return {**data, "tx": [add(t) for t in data["tx"]]}
    return data


def make_handler(station):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode())

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/help", "/card"):         # the printable two-sided reference card (station/static/card.html)
                page = b'<!doctype html><html lang="en"><head><meta charset="utf-8">' \
                       b'<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>' + \
                       (STATIC / "card.html").read_bytes() + b"</body></html>"
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(with_who("snapshot", station.snapshot()))
            if u.path == "/api/lookup":
                call = (parse_qs(u.query).get("call") or [""])[0].upper()
                if not re.fullmatch(r"[A-Z0-9/]{3,12}", call) or not station.callbook:
                    return self._json({"error": "bad callsign"}, 400)
                return self._json(station.callbook.card(call, qrz_network=station.cfg.get("qrz_online", True)))
            if u.path == "/api/test":
                from .wer import TEST_TEXT
                return self._json({"text": TEST_TEXT, "history": station.test_history()})
            if u.path == "/api/settings":
                return self._json(station.operator())
            if u.path == "/api/schedule":                        # schedule: the nets, for the editor
                return self._json(station.nets.list())
            if u.path.startswith("/audio/"):
                tid = u.path.rsplit("/", 1)[-1]
                tx = station._find(tid)
                p = (station.data / tx["wav"]).resolve() if tx and tx.get("wav") else None
                if not p or not p.is_file() or station.data.resolve() not in p.parents:
                    return self._json({"error": "no audio"}, 404)
                return self._send(200, p.read_bytes(), "audio/wav")
            if u.path == "/events":
                return self._events()
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            m = re.fullmatch(r"/api/radios/([\w-]+)(?:/(pause|clear|listen))?", u.path)
            who = re.fullmatch(r"/api/tx/(\w+)/speaker", u.path)
            routes = {"/api/settings", "/api/winlink/preview", "/api/winlink/send", "/api/accurate", "/api/test"}
            sched = re.fullmatch(r"/api/schedule(/[\w-]+(/delete)?)?", u.path)     # schedule: add / edit / delete
            # JSON only: a plain cross-site form cannot send it, so another web page cannot act for us
            if not (m or sched or who or u.path in routes) or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "not found"}, 404)
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 512 * 1024)
                body = json.loads(self.rfile.read(n) or b"{}")
                if sched:
                    return self._json(station.nets.post(u.path, body))
                if who:                                         # the operator's "Who?" ({"call": ""} = automatic)
                    try:
                        return self._json(with_who("tx", station.set_speaker(who.group(1), body.get("call"))))
                    except LookupError:
                        return self._json({"error": "no such transmission"}, 404)
                if u.path == "/api/settings":
                    return self._json(station.set_operator(body))
                if u.path == "/api/winlink/preview":
                    return self._json(station.winlink_preview(body.get("radio"), body.get("start"), body.get("end"),
                                                              body.get("title")))
                if u.path == "/api/winlink/send":
                    return self._json(station.winlink_send(body))
                if u.path == "/api/test":
                    return self._json(station.score_test(body.get("radio"), body.get("tx")))
                if u.path == "/api/accurate":
                    return self._json(station.accurate_now(body.get("radio"), body.get("start"), body.get("end"),
                                                           bool(body.get("force"))))
                if m.group(2) == "pause":
                    return self._json(station.set_paused(m.group(1), bool(body.get("paused", True))))
                if m.group(2) == "listen":                       # schedule: listen only during nets
                    return self._json(station.nets.set_listen(m.group(1), body))
                if m.group(2) == "clear":
                    return self._json(station.clear_radio(m.group(1)))
                return self._json(station.set_radio(m.group(1), body.get("freq_mhz"), body.get("mode")))
            except (RuntimeError, FileNotFoundError) as e:
                return self._json({"error": str(e)}, 409)
            except NoSuchNet:
                return self._json({"error": "no such net"}, 404)
            except KeyError:
                return self._json({"error": "no such radio"}, 404)
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e) or "bad request"}, 400)

        def _events(self):
            q = station.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self._emit("snapshot", with_who("snapshot", station.snapshot()))
                while True:
                    try:
                        kind, data = q.get(timeout=15)
                        self._emit(kind, with_who(kind, data))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                station.bus.unsubscribe(q)
                self.close_connection = True

        def _emit(self, kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n".encode())
            self.wfile.flush()

    return H


def serve(station, host="0.0.0.0", port=8091) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(station))
    srv.daemon_threads = True
    return srv
