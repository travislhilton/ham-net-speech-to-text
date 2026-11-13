"""Grok speech-to-text as a COMPARED engine (owner 2026-11-10: "to get us started to determine the best approach").

The station's own engine stays the transcriber. On the radios the operator ticks (Settings → Grok speech-to-text,
off by default) each finished transmission's recording is also sent to xAI's speech-to-text (POST /v1/stt, the
station's XAI_API_KEY), live, as it is heard. The answer is kept beside the transmission (data/grok_stt/<day>.jsonl,
never in the day log) and compared with the station's texts: GET /api/grokstt/compare.

Interactive, never queued (owner: "No need to queue Grok jobs when off line"): a transmission heard while xAI cannot be
reached, while two requests are already out, or past the day's minutes cap is SKIPPED (counted, never sent later).
After a network failure xAI is not tried for a back-off (station/online.py).

The comparison's reference is the best text the station has: the operator's correction when there is one (counted
apart: it is the only human truth), else the accurate pass (large-v3 with the net's context). It is not a human
transcript, so the numbers say how far each engine is from the final pass, and the corrected rows say who was right.

Sending audio off the PC: the recordings are of other licensed stations on an amateur frequency (public by rule,
97.113(a)(4) no codes), but they leave the station, so the operator chooses the radios. Price: xAI did not publish a
per-minute price on the speech pages (2026-11-10) — the minutes cap (default 60 a day) bounds the spend.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from . import secrets
from .online import Online, is_network_error

URL = "https://api.x.ai/v1/stt"
MODEL = "grok-voice-transcribe-2.0"
KEY = "XAI_API_KEY"
TIMEOUT_S = 30
MAX_CLIP_S = 120          # a longer clip is an open squelch, not speech worth paying for
MAX_INFLIGHT = 2          # more at once = skipped, never waited for
KEYTERMS_MAX = 100        # xAI's limits: 100 terms, 50 characters each


def multipart(fields: list[tuple[str, str]], wav: bytes, filename="clip.wav") -> tuple[bytes, str]:
    """multipart/form-data with `file` LAST (xAI: fields after the file may be ignored)."""
    b = "----hamsidekick" + uuid.uuid4().hex
    out = []
    for k, v in fields:
        out.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.append(f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
               f"Content-Type: audio/wav\r\n\r\n".encode() + wav + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


def call_stt(key: str, wav: bytes, language="en", keyterms=(), timeout=TIMEOUT_S) -> dict:
    fields = [("model", MODEL)] + ([("language", language)] if language else [])
    fields += [("keyterm", t) for t in list(keyterms)[:KEYTERMS_MAX]]
    body, ctype = multipart(fields, wav)
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def keyterms_for(call: str, local_entries, net_name=None) -> list[str]:
    """Words the station expects: its own call, the area's local names and their web sites, the net's name."""
    out = [call] if call else []
    for e in local_entries or []:
        out.append(e["name"])
        if e.get("meaning"):
            out.append(e["meaning"])
        if e.get("web"):
            out.append(e["web"])
    if net_name:
        out.append(net_name)
    seen, keep = set(), []
    for t in out:
        t = " ".join(str(t).split())[:50]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            keep.append(t)
    return keep[:KEYTERMS_MAX]


_NUMW = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
         "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
_SPELL_TOK = r"(?:[A-Za-z]|\d|zero|one|two|three|four|five|six|seven|eight|nine|niner)"
_SPELLED = re.compile(r"(?<![\w'])(" + _SPELL_TOK + r"(?:[\s,-]+" + _SPELL_TOK + r"){3,6})(?![\w'])", re.I)
_CALL = re.compile(r"^[AKNW][A-Z]?\d[A-Z]{1,3}$")


def join_spelled(text: str) -> str:
    """Grok writes a call letter by letter ("K I 5 F U N", "N seven T D I"): joined when the run is a US call shape
    (the station's engine writes them joined, and the callsign reader expects that)."""
    def rep(m):
        toks = [t for t in re.split(r"[\s,-]+", m.group(1)) if t]
        sym = [_NUMW.get(t.lower(), t.upper()) for t in toks]
        if any(len(x) != 1 for x in sym):
            return m.group(0)
        for i in range(len(sym)):                        # the longest call shape inside the run
            for j in range(len(sym), i + 3, -1):
                c = "".join(sym[i:j])
                if _CALL.match(c):
                    pre = " ".join(toks[:i])
                    post = " ".join(toks[j:])
                    return " ".join(x for x in (pre, c, post) if x)
        return m.group(0)
    return _SPELLED.sub(rep, text or "")


def best_reference(t: dict) -> tuple[str | None, str]:
    """(text, source): the operator's correction, else the accurate pass, else none."""
    c = t.get("corrected")
    if c and not c.get("noise") and c.get("text"):
        return c["text"], "corrected"
    a = t.get("accurate")
    if a and a.get("text"):
        return a["text"], "accurate"
    return None, ""


def day_rows(path: Path) -> list[dict]:
    """A day log's transmissions with the updates that matter here applied (accurate, corrected)."""
    txs, by_id = [], {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "update" in e:
            t = by_id.get(str(e["update"]))
            if t is not None:
                for k in ("accurate", "corrected", "accurate_echo"):
                    if k in e:
                        if e[k] is None:
                            t.pop(k, None)
                        else:
                            t[k] = e[k]
                if "accurate_echo" in e:
                    t.pop("accurate", None)
            continue
        if e.get("id") is not None:
            if str(e["id"]) in by_id:
                by_id[str(e["id"])].update(e)
            else:
                by_id[str(e["id"])] = e
                txs.append(e)
    return txs


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


class GrokSTT:
    """Settings in data/grok_stt.json: {"radios": {rid: true}, "minutes_per_day": 60, "language": "en",
    "keyterms": true}. Results in data/grok_stt/<YYYYMMDD>.jsonl."""

    def __init__(self, station, call=call_stt):
        self.st = station
        self.call = call
        self.path = station.data / "grok_stt.json"
        self.dir = station.data / "grok_stt"
        self.lock = threading.Lock()
        self.cfg = {"radios": {}, "minutes_per_day": 60, "language": "en", "keyterms": True}
        try:
            self.cfg.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.online = Online("xai-stt")
        self.inflight = 0
        self.today = {"day": "", "seconds": 0.0, "sent": 0, "skipped": {}}
        self.last: dict | None = None

    # ----- settings -----
    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def settings(self, body: dict) -> dict:
        with self.lock:
            if "radios" in body:
                r = body["radios"] or {}
                if not isinstance(r, dict) or any(k not in self.st.radios for k in r):
                    raise ValueError("radios: {radio id: true/false} for this station's radios")
                self.cfg["radios"] = {k: bool(v) for k, v in r.items() if v}
            if "minutes_per_day" in body:
                m = int(body["minutes_per_day"])
                if not 0 <= m <= 1440:
                    raise ValueError("minutes a day: 0 to 1440")
                self.cfg["minutes_per_day"] = m
            if "language" in body:
                v = str(body["language"] or "").strip()
                if v and not re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", v):
                    raise ValueError("language: a code like en or es-MX (empty = detect)")
                self.cfg["language"] = v
            if "keyterms" in body:
                self.cfg["keyterms"] = bool(body["keyterms"])
            self._save()
        return self.view()

    def enabled(self, rid) -> bool:
        return bool((self.cfg.get("radios") or {}).get(rid))

    def _roll(self, now):
        day = now.strftime("%Y%m%d")
        if self.today["day"] != day:
            self.today = {"day": day, "seconds": self._used_seconds(day), "sent": 0, "skipped": {}}

    def _used_seconds(self, day) -> float:
        return sum(float(r.get("duration_s") or 0) for r in self.results(day) if not r.get("error"))

    def view(self) -> dict:
        now = self.st.clock()
        with self.lock:
            self._roll(now)
            t = dict(self.today)
        return {"settings": dict(self.cfg), "model": MODEL, "key_set": bool(secrets.read(self.st.root, KEY)),
                "online": self.online.view(), "today": t | {"minutes": round(t["seconds"] / 60, 1)},
                "inflight": self.inflight, "last": self.last}

    # ----- hearing -----
    def _skip(self, why):
        with self.lock:
            self.today["skipped"][why] = self.today["skipped"].get(why, 0) + 1

    def on_final(self, tx: dict):
        """A transmission was transcribed: send its recording now, or skip it (never queued)."""
        rid = tx.get("radio")
        if not self.enabled(rid) or not tx.get("wav"):
            return
        dur = float(tx.get("duration_s") or 0)
        if tx.get("noise"):
            return self._skip("noise")
        if dur > MAX_CLIP_S:
            return self._skip("longer than 2 minutes")
        if not secrets.read(self.st.root, KEY):
            return self._skip("no xAI key")
        if not self.online.should_try():
            return self._skip("offline")
        now = self.st.clock()
        with self.lock:
            self._roll(now)
            if self.today["seconds"] + dur > 60 * float(self.cfg.get("minutes_per_day") or 0):
                self.today["skipped"]["over the day's minutes"] = self.today["skipped"].get("over the day's minutes", 0) + 1
                return
            if self.inflight >= MAX_INFLIGHT:
                self.today["skipped"]["busy"] = self.today["skipped"].get("busy", 0) + 1
                return
            self.inflight += 1
            self.today["seconds"] += dur
        th = threading.Thread(target=self._run_safe, args=(dict(tx),), daemon=True)
        th.start()
        return th

    def _run_safe(self, tx):
        try:
            self.run(tx)
        except Exception as e:  # noqa: BLE001 - the comparison never stops the station
            print(f"grok stt {tx.get('id')}: {type(e).__name__}: {e}", flush=True)
        finally:
            with self.lock:
                self.inflight -= 1

    def run(self, tx: dict) -> dict:
        """Transcribe one transmission's recording with Grok and keep the answer (also used for chosen past ones)."""
        key = secrets.read(self.st.root, KEY)
        wav = (self.st.data / tx["wav"]).read_bytes()
        terms = []
        if self.cfg.get("keyterms", True):
            try:
                from . import localnames
                entries = localnames.load(self.st.data)
            except Exception:  # noqa: BLE001
                entries = []
            terms = keyterms_for(self.st.me, entries, (tx.get("net") or {}).get("name"))
        t0 = time.monotonic()
        rec = {"id": tx["id"], "radio": tx.get("radio"), "start": tx.get("start"),
               "duration_s": tx.get("duration_s"), "model": MODEL, "at": datetime.now().isoformat(timespec="seconds")}
        try:
            out = self.call(key, wav, self.cfg.get("language") or "", terms)
        except Exception as e:  # noqa: BLE001
            rec["took_s"] = round(time.monotonic() - t0, 2)
            if is_network_error(e):
                self.online.failed(e)
            else:
                self.online.ok()
            rec["error"] = (f"xAI answered {e.code}" if isinstance(e, urllib.error.HTTPError)
                            else f"{type(e).__name__}: {str(e)[:120]}")
            self._keep(rec)
            return rec
        self.online.ok()
        text = str(out.get("text") or "").strip()
        text_n, calls = self._calls(text)
        rec["text_normalized"] = text_n
        words = [{k: w[k] for k in ("text", "start", "end", "confidence") if k in w} for w in out.get("words") or []]
        rec.update(took_s=round(time.monotonic() - t0, 2), text=text, callsigns=calls,
                   language=out.get("language"), audio_s=out.get("duration"), words=words, keyterms=len(terms))
        self._keep(rec)
        return rec

    def _keep(self, rec):
        day = str(rec.get("start") or "")[:10].replace("-", "") or datetime.now().strftime("%Y%m%d")
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / f"{day}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.today["sent"] += 1 if day == self.today["day"] else 0
            self.last = {k: rec.get(k) for k in ("id", "radio", "start", "text", "error", "took_s")}
        try:
            self.st.bus.publish("grokstt", {"result": self.last})
        except Exception:  # noqa: BLE001
            pass

    def _calls(self, text: str) -> tuple[str, list[str]]:
        """Grok's text as the station reads calls: spelled runs joined, then the station's own callsign reader."""
        joined = join_spelled(text)
        try:
            from hamkit.callsigns import normalize
            validate = self.st.callbook.validate if getattr(self.st, "callbook", None) else None
            return normalize(joined, roster=getattr(self.st, "roster", set()), validate=validate)
        except Exception:  # noqa: BLE001
            return joined, []

    def results(self, day: str) -> list[dict]:
        p = self.dir / f"{day}.jsonl"
        out = {}
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") is not None and (not r.get("error") or str(r["id"]) not in out):
                    out[str(r["id"])] = r                         # the latest good answer per transmission
        except OSError:
            return []
        return list(out.values())

    def run_past(self, ids: list, limit=50) -> dict:
        """POST /api/grokstt/run {"ids": [...]}: Grok on chosen transmissions now (the comparison on a past net).
        Runs while the request waits; offline = refused, not queued."""
        if not secrets.read(self.st.root, KEY):
            raise ValueError("set the xAI key first (Settings → Station agent)")
        if not self.online.should_try():
            raise ValueError(f"xAI is offline ({self.online.last_error}); nothing was sent")
        done, failed = 0, 0
        for tid in [str(i) for i in ids][:limit]:
            tx = self.st._find(tid) if hasattr(self.st, "_find") else None
            if not tx or not tx.get("wav") or float(tx.get("duration_s") or 0) > MAX_CLIP_S:
                failed += 1
                continue
            r = self.run(dict(tx))
            done += 0 if r.get("error") else 1
            failed += 1 if r.get("error") else 0
            if r.get("error") and not self.online.should_try():
                break
        return {"done": done, "failed": failed, **self.view()}

    # ----- comparing -----
    def compare(self, day: str | None = None, rows: list[dict] | None = None, radio: str | None = None) -> dict:
        """Grok vs the station's live text, both measured against the best reference."""
        from .wer import wer
        day = day or self.st.clock().strftime("%Y%m%d")
        if rows is None:
            rows = (list(self.st.tx) if day == self.st.clock().strftime("%Y%m%d")
                    else day_rows(self.st.data / "logs" / f"{day}.jsonl"))
        grok = {str(r["id"]): r for r in self.results(day) if not r.get("error")}
        me = self.st.me or None
        items, agg = [], {}
        for t in rows:
            g = grok.get(str(t.get("id")))
            if not g or (radio and t.get("radio") != radio):
                continue
            ref, src = best_reference(t)
            it = {"id": t["id"], "radio": t.get("radio"), "start": t.get("start"), "duration_s": t.get("duration_s"),
                  "live": t.get("text"), "grok": g.get("text"), "ref": ref, "ref_from": src,
                  "live_s": t.get("took_s"), "grok_s": g.get("took_s"), "noise": bool(t.get("noise")),
                  "live_calls": t.get("callsigns") or [], "grok_calls": self._calls(g.get("text") or "")[1],
                  "ref_calls": ((t.get("corrected") or t.get("accurate") or {}).get("callsigns") or [])}
            if ref:
                it["live_wer"] = wer(ref, t.get("text") or "", me)
                it["grok_wer"] = wer(ref, g.get("text") or "", me)
            items.append(it)
            for k in (src or "none", "all"):
                a = agg.setdefault(k, {"n": 0, "words": 0, "live_err": 0, "grok_err": 0, "calls": 0, "live_calls": 0,
                                       "grok_calls": 0, "live_s": [], "grok_s": []})
                a["n"] += 1
                if ref:
                    a["words"] += it["live_wer"]["words"]
                    a["live_err"] += it["live_wer"]["errors"]
                    a["grok_err"] += it["grok_wer"]["errors"]
                rc = set(it["ref_calls"])
                a["calls"] += len(rc)
                a["live_calls"] += len(rc & set(it["live_calls"]))
                a["grok_calls"] += len(rc & set(it["grok_calls"]))
                if it["live_s"] is not None:
                    a["live_s"].append(float(it["live_s"]))
                if it["grok_s"] is not None:
                    a["grok_s"].append(float(it["grok_s"]))
        summary = {}
        for k, a in agg.items():
            summary[k] = {"n": a["n"], "words": a["words"],
                          "live_wer": round(a["live_err"] / a["words"], 3) if a["words"] else None,
                          "grok_wer": round(a["grok_err"] / a["words"], 3) if a["words"] else None,
                          "calls": a["calls"],
                          "live_call_recall": round(a["live_calls"] / a["calls"], 3) if a["calls"] else None,
                          "grok_call_recall": round(a["grok_calls"] / a["calls"], 3) if a["calls"] else None,
                          "live_s_p50": _pct(a["live_s"], .5), "grok_s_p50": _pct(a["grok_s"], .5),
                          "grok_s_p95": _pct(a["grok_s"], .95)}
        items.sort(key=lambda i: str(i.get("start") or ""))
        return {"day": day, "summary": summary, "items": items,
                "note": "measured against the operator's correction (human) or the accurate pass (large-v3, not a "
                        "human transcript); call recall = the reference's calls each engine also found"}
