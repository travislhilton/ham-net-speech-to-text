"""Dashboard server (standard library only): the page, a Server-Sent Events stream, state, lookups, audio.
Reach it from the station PC (http://localhost:8091/) or any PC on the LAN / Tailscale (the installer's
firewall rule allows only those)."""
import json
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

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
                from . import phone
                page = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
                       '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>' + \
                       phone.card_with_download((STATIC / "card.html").read_text(encoding="utf-8"),
                                                station.cfg.get("download_url")) + "</body></html>"
                page = page.encode("utf-8")
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/phone":                         # "Open on a phone": addresses + QR codes
                from . import phone
                port = self.server.server_address[1]
                out = []
                for x in phone.addresses(self.headers.get("Host"), port):
                    try:
                        x["svg"] = phone.qr_svg(x["url"])
                    except ImportError:                    # segno missing (old install): the URL alone still helps
                        x["svg"] = ""
                    out.append(x)
                return self._json({"addresses": out})
            if u.path == "/api/state":
                return self._json(with_who("snapshot", station.snapshot()))
            if u.path == "/api/radio-list":                        # Radios…: the list + the sound inputs plugged in
                return self._json(station.radio_list())
            lv = re.fullmatch(r"/api/radios/([\w-]+)/level", u.path)          # Set level…: meter vs target
            if lv:
                try:
                    return self._json(station.level(lv.group(1), (parse_qs(u.query).get("step") or ["noise"])[0]))
                except KeyError:
                    return self._json({"error": "no such radio"}, 404)
            vs = re.fullmatch(r"/api/tx/(\w+)/voice", u.path)
            if vs:                                              # Who?'s "sounds like …" (station/voicehint.py)
                try:
                    return self._json(station.voice_suggest(vs.group(1)))
                except LookupError:
                    return self._json({"error": "no such transmission"}, 404)
            if u.path == "/api/lookup":
                call = (parse_qs(u.query).get("call") or [""])[0].upper()
                if not re.fullmatch(r"[A-Z0-9/]{3,12}", call) or not station.callbook:
                    return self._json({"error": "bad callsign"}, 400)
                return self._json(station.callbook.card(call, qrz_network=station.cfg.get("qrz_online", True)))
            if u.path == "/api/test":
                from .wer import test_text
                return self._json({"text": test_text(station.cfg.get("callsign")), "history": station.test_history()})
            if u.path == "/api/settings":
                return self._json(station.operator())
            if u.path in ("/readout", "/readout.html"):         # the read-out: approve, then the large-type card
                return self._send(200, (STATIC / "readout.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/readout":
                return self._json(station.readout.view())
            if u.path == "/api/scan":                            # HF scan: each radio's mode + antenna, remembered spots
                return self._json(station.scan.view())
            if u.path == "/api/agent":                           # the station agent: settings + drafts
                return self._json(station.agent.view())
            if u.path == "/api/secrets":                         # Settings: which keys are set (never the keys)
                from . import secrets
                return self._json({"keys": [secrets.status(station.root, k) for k in secrets.KEYS]})
            if u.path == "/api/learned":                         # Learned fixes (station/corrections.py)
                return self._json(station.learned())
            if u.path == "/api/accuracy":                        # accuracy from the operator's corrections
                return self._json(station.accuracy())
            if u.path == "/api/schedule":                        # schedule: the nets, for the editor
                return self._json(station.nets.list())
            if u.path.startswith(("/api/search", "/api/history", "/api/nets/", "/api/station/", "/api/alerts")):
                return self._history_get(u)
            if u.path.startswith("/audio/"):
                tid = u.path.rsplit("/", 1)[-1]
                tx = station._find(tid)
                wav = tx.get("wav") if tx else (station.history.tx_wav(tid) if re.fullmatch(r"\w+", tid) else None)
                p = (station.data / wav).resolve() if wav else None
                if not p or not p.is_file() or station.data.resolve() not in p.parents:
                    return self._json({"error": "no audio"}, 404)
                return self._send(200, p.read_bytes(), "audio/wav")
            if u.path == "/events":
                return self._events()
            self._json({"error": "not found"}, 404)

        def _history_get(self, u):
            """History, search, analysis, still open, alerts (all read-only)."""
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            h = station.history
            try:
                if u.path == "/api/search":
                    return self._json(h.search(q.get("q", ""), q.get("net"), q.get("radio"), q.get("from"), q.get("to"),
                                               q.get("call"), min(int(q.get("limit") or 50), 500), q.get("kind")))
                if u.path == "/api/history":
                    return self._json({"instances": h.instances(q.get("net"), q.get("radio"), q.get("kind"),
                                                                int(q.get("limit") or 200), q.get("before")),
                                       "status": h.status})
                if u.path == "/api/history/instance":
                    return self._json(h.instance(q.get("key", "")))
                if u.path == "/api/history/minutes":            # export: Markdown or plain text, as a download
                    md = q.get("format", "md") != "txt"
                    body = h.minutes_text(q.get("key", ""), md).encode("utf-8")
                    name = re.sub(r"[^\w.-]+", "_", q.get("key", "minutes")) + (".md" if md else ".txt")
                    return self._send(200, body, "text/markdown; charset=utf-8" if md else "text/plain; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="minutes-{name}"'})
                if u.path == "/api/nets/analysis":
                    return self._json(h.analysis(q.get("net", "")))
                if u.path == "/api/nets/open":
                    return self._json(h.still_open(q.get("net") or None))
                if u.path == "/api/station/nets":
                    return self._json({"call": q.get("call", "").upper(), "nets": h.station_nets(q.get("call", ""))})
                if u.path == "/api/alerts":
                    return self._json({"alerts": station.nets.alerts.active()})
            except LookupError:
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e) or "bad request"}, 400)
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            m = re.fullmatch(r"/api/radios/([\w-]+)(?:/(pause|clear|listen|level|setup))?", u.path)
            who = re.fullmatch(r"/api/tx/(\w+)/speaker", u.path)
            fix = re.fullmatch(r"/api/tx/(\w+)/correct", u.path)                                        # ✎ Fix
            routes = {"/api/radio-list", "/api/settings", "/api/winlink/preview", "/api/winlink/send", "/api/accurate", "/api/test",
                      "/api/history/minutes", "/api/learned"}
            sched = re.fullmatch(r"/api/schedule(/[\w-]+(/(delete|skip|move|restore|radio|settle|keep))?)?", u.path)   # schedule
            alert = re.fullmatch(r"/api/alerts/([\w@.%-]+)/(ready|snooze|skip|dismiss)", u.path)        # pre-net alert
            item = re.fullmatch(r"/api/items(?:/(\d+)(/delete)?)?", u.path)                             # minutes items
            close = re.fullmatch(r"/api/netclose/([\w-]+)/(end|undo|save_ncs)", u.path)                  # end of a net
            sec = re.fullmatch(r"/api/secrets/([A-Z_]+)(/test)?", u.path)                               # Settings: a key
            agent = re.fullmatch(r"/api/agent/(settings|radio|ask|rate)(/[\w-]+)?", u.path)               # station agent
            scan = re.fullmatch(r"/api/scan/[\w-]+(/antenna)?", u.path)                                   # HF scan
            rdo = re.fullmatch(r"/api/readout/(fetch|load|approve|read|url)", u.path)                      # read-out
            # JSON only: a plain cross-site form cannot send it, so another web page cannot act for us
            if not (m or sched or who or fix or alert or item or close or sec or agent or scan or rdo or u.path in routes) or not self.headers.get("Content-Type", "").startswith("application/json"):
                self.close_connection = True                   # its body is unread: never reuse the connection
                return self._json({"error": "not found"}, 404)
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 512 * 1024)
                body = json.loads(self.rfile.read(n) or b"{}")
                if agent:
                    return self._json(station.agent.post(u.path, body))
                if scan:
                    return self._json(station.scan.post(u.path, body))
                if rdo:
                    return self._json(station.readout.post(u.path, body))
                if sched:
                    return self._json(station.nets.post(u.path, body))
                if sec:                                          # {"value": key | null}; /test asks the provider
                    from . import secrets
                    if sec.group(1) not in secrets.KEYS:
                        return self._json({"error": "unknown key"}, 404)
                    if sec.group(2):
                        return self._json(secrets.test(station.root, sec.group(1)))
                    return self._json(secrets.write(station.root, sec.group(1), body.get("value")))
                if close:                                       # End net / Not closed / Save net control
                    rid, act, key = close.group(1), close.group(2), str(body.get("key") or "") or None
                    if act == "end":
                        station.closer.end_now(rid, key)
                    elif act == "undo":
                        station.closer.undo(rid, key or "")
                    else:
                        station.closer.save_ncs(rid, key or "", body.get("call"))
                    return self._json(station.closer.state())
                if alert:
                    return self._json(station.nets.alerts.act(unquote(alert.group(1)), alert.group(2)))
                if item:
                    if not item.group(1):
                        return self._json(station.history.add_item(body))
                    if item.group(2):
                        return self._json(station.history.delete_item(int(item.group(1))))
                    return self._json(station.history.update_item(int(item.group(1)), body))
                if u.path == "/api/history/minutes":            # notes, net control, attendance corrections
                    return self._json(station.history.set_minutes(str(body.get("key") or ""), body))
                if who:                                         # the operator's "Who?" ({"call": ""} = automatic)
                    try:
                        return self._json(with_who("tx", station.set_speaker(who.group(1), body.get("call"),
                                                                                  body.get("suggested"))))
                    except LookupError:
                        return self._json({"error": "no such transmission"}, 404)
                if fix:                                         # {"text": ...} or {"undo": true}
                    try:
                        return self._json(with_who("tx", station.correct(fix.group(1), body.get("text"),
                                                                         undo=bool(body.get("undo")))))
                    except LookupError:
                        return self._json({"error": "no such transmission"}, 404)
                if u.path == "/api/learned":                    # {"action": accept|ignore|unignore|remove, heard, say}
                    return self._json(station.learned(str(body.get("action") or ""), str(body.get("heard") or ""),
                                                      str(body.get("say") or "")) if body.get("action")
                                      else station.learned())
                if u.path == "/api/settings":
                    return self._json(station.set_operator(body))
                if u.path == "/api/radio-list":                   # Radios…: save the list (the station restarts)
                    return self._json(station.save_radios(body))
                if u.path == "/api/winlink/preview":
                    return self._json(station.winlink_preview(body.get("radio"), body.get("start"), body.get("end"),
                                                              body.get("title")))
                if u.path == "/api/winlink/send":
                    return self._json(station.winlink_send(body))
                if u.path == "/api/test":
                    return self._json(station.score_test(body.get("radio"), body.get("tx")))
                if u.path == "/api/accurate" and body.get("ids"):          # older transmissions, by id
                    return self._json({"jobs": station.accurate_past(body["ids"])})
                if u.path == "/api/accurate":
                    return self._json(station.accurate_now(body.get("radio"), body.get("start"), body.get("end"),
                                                           bool(body.get("force"))))
                if m.group(2) == "pause":
                    return self._json(station.set_paused(m.group(1), bool(body.get("paused", True))))
                if m.group(2) == "listen":                       # schedule: listen only during nets
                    return self._json(station.nets.set_listen(m.group(1), body))
                if m.group(2) == "clear":
                    return self._json(station.clear_radio(m.group(1)))
                if m.group(2) == "setup":                        # the card: antenna (HF | VHF/UHF), nets | Winlink only
                    return self._json(station.set_radio_setup(m.group(1), body))
                if m.group(2) == "level":                        # Set level…: {"action": "auto"} | {"db"} | {"step"}
                    return self._json(station.set_level(m.group(1), body))
                cat = getattr(station, "cat", None)
                if cat is not None and cat.has(m.group(1)):        # a CAT radio: Change tunes the radio itself
                    return self._json(cat.tune_now(m.group(1), body.get("freq_mhz"), body.get("mode")))
                return self._json(station.set_radio(m.group(1), body.get("freq_mhz"), body.get("mode")))
            except (RuntimeError, FileNotFoundError) as e:
                return self._json({"error": str(e)}, 409)
            except NoSuchNet:
                return self._json({"error": "no such net"}, 404)
            except KeyError:
                return self._json({"error": "no such radio"}, 404)
            except LookupError:
                return self._json({"error": "not found"}, 404)
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e) or "bad request"}, 400)

        def _events(self):
            q = station.bus.subscribe()
            station.viewer(True)                               # a dashboard is open (keep awake + engine check)
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
                station.viewer(False)
                self.close_connection = True

        def _emit(self, kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n".encode())
            self.wfile.flush()

    return H


def serve(station, host="0.0.0.0", port=8091) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(station))
    srv.daemon_threads = True
    return srv
