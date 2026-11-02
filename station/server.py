"""Dashboard server (standard library only): the page, a Server-Sent Events stream, state, lookups, audio.
Reach it from the station PC (http://localhost:8091/) or any PC on the LAN / Tailscale (the installer's
firewall rule allows only those)."""
import json
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC = Path(__file__).parent / "static"


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
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(station.snapshot())
            if u.path == "/api/lookup":
                call = (parse_qs(u.query).get("call") or [""])[0].upper()
                if not re.fullmatch(r"[A-Z0-9/]{3,12}", call) or not station.callbook:
                    return self._json({"error": "bad callsign"}, 400)
                return self._json(station.callbook.card(call, qrz_network=station.cfg.get("qrz_online", True)))
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
            m = re.fullmatch(r"/api/radios/([\w-]+)", u.path)
            # JSON only: a plain cross-site form cannot send it, so another web page cannot retune us
            if not m or not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._json({"error": "not found"}, 404)
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(n) or b"{}")
                return self._json(station.set_radio(m.group(1), body.get("freq_mhz"), body.get("mode")))
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
                self._emit("snapshot", station.snapshot())
                while True:
                    try:
                        kind, data = q.get(timeout=15)
                        self._emit(kind, data)
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
