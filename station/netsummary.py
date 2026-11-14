"""The AI paragraph at the top of a net's minutes (owner 2026-11-14: "if the xAI api key is present do the AI generated
summaries by default. let there be a setting that can be turned off").

The rules always write a few summary lines (History.summary_lines). With the xAI key present and Settings ->
"AI summary of each net's minutes" on (the default), this worker also writes a short paragraph for each net once it
is over: from the drafted minutes and the transcript, told to say only what they say. It is written again when the
text it was written from changes (the accurate pass, the operator's fixes and check-in marks), never over the
operator's own edit; **Rewrite** on the minutes asks for a new one at once. The paragraph goes in the minutes, the
.md / text export and the ICS-213 (History.summary_text), marked AI-drafted.

Costs: one call per net (grok fast model, about 3-6k tokens in, 200 out: well under a cent); at most MAX_PER_HOUR.
Offline or no key: the rule lines only, nothing is queued.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

MODEL = "grok-4.20-0309-non-reasoning"
TIMEOUT_S = 60
QUIET_MIN = 10            # a net is "over" this long after its last transmission (or once it is closed)
LOOK_BACK_H = 36          # nets older than this are summarized only on Rewrite (no surprise backfill)
EVERY_S = 60
MAX_PER_HOUR = 30
MAX_CHARS = 14000         # the transcript part of the prompt, at most (a 2-hour net is cut from the middle)
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
          "additionalProperties": False}
SYSTEM = ("You write the summary paragraph at the top of an amateur radio net's minutes. Use only what the minutes "
          "and the transcript say: never invent a callsign, name, number, place or time. 3 to 5 plain sentences, "
          "under 120 words, past tense: what kind of net it was and who ran it, how many took part, then the things "
          "a member who missed it needs to know (urgent matters, traffic passed, announcements, upcoming events, "
          "action items with who, decisions). Machine transcription has errors: leave out anything you are unsure "
          "of rather than guess. No greeting, no headings, no lists, no Markdown.")


def enabled(st) -> bool:
    """The setting (on unless turned off) and an xAI key."""
    if (st._saved().get("operator") or {}).get("ai_summary") is False:
        return False
    from . import secrets
    try:
        return bool(secrets.read(st.root, "XAI_API_KEY"))
    except Exception:  # noqa: BLE001
        return False


def prompt(h, key: str, d: dict) -> str:
    rules = "\n".join(h.summary_lines(key, d))
    roll = [p for p in d["participants"] if p["role"] in ("ncs", "checkin")]
    fmt = lambda p: p["call"] + (f" ({p['name']})" if p.get("name") else "") + (" net control" if p["role"] == "ncs" else "")
    people = ", ".join(fmt(p) for p in roll if p.get("confirmed")) or "(none confirmed)"
    once = ", ".join(p["call"] for p in roll if not p.get("confirmed"))
    items = "\n".join(f"- {i['kind']}: {i['text']}" + (f" (who: {i['who']})" if i.get("who") else "")
                      for i in d["items"] if i["status"] != "dismissed") or "(none drafted)"
    tx = [f"{(t.get('start') or '')[11:16]} {t.get('speaker') or '?'}: {t.get('text') or ''}"
          for t in d["tx"] if not t.get("noise") and (t.get("text") or "").strip()]
    body = "\n".join(tx)
    if len(body) > MAX_CHARS:                               # keep the start and the end: the opening and the close
        half = MAX_CHARS // 2
        body = body[:half] + "\n[... middle of the net left out ...]\n" + body[-half:]
    when = f"{(d.get('start') or '')[:16].replace('T', ' ')} to {(d.get('end') or '')[11:16]}"
    return (f"Net: {d.get('name') or 'Conversation'}" + (" (emergency / priority)" if d.get("emergency") else "")
            + f"\nWhen: {when}" + (f", {d['freq_mhz']:.3f} MHz" if d.get("freq_mhz") else "")
            + f"\nNet control: {d.get('ncs') or 'not identified'}\nTook part (confirmed): {people}"
            + (f"\nHeard once, not confirmed (often mis-heard calls: do not count or name them): {once}" if once else "")
            + "\n\nThe station's summary lines:\n"
            + rules + f"\n\nDrafted minute items:\n{items}\n\nTranscript (time, speaker, words):\n{body}")


class Summarizer:
    def __init__(self, st, call=None):
        self.st = st
        self._call = call                       # tests: (system, user) -> text
        self.lock = threading.Lock()
        self.calls: list[float] = []
        self.last_error: str | None = None

    def _ask(self, system: str, user: str) -> str:
        if self._call:
            return self._call(system, user)
        from . import agent, secrets
        key = secrets.read(self.st.root, "XAI_API_KEY")
        if not key:
            raise RuntimeError("no xAI key (Settings → Station agent)")
        out, _ = agent.call_xai(key, MODEL, system, user, timeout=TIMEOUT_S, schema=SCHEMA, max_tokens=500,
                                name="net_summary")
        return str(out.get("summary") or "").strip()

    def write(self, key: str, force=False) -> dict | None:
        """Write (or, with force, rewrite) the AI paragraph for one net or conversation. Returns the stored one."""
        h = self.st.history
        with self.lock:
            d = h.instance(key)
            sig = h.summary_sig(key, d)
            cur = h.summary_ai(key)
            if not force and cur and (cur["edited"] or cur["sig"] == sig):
                return cur
            self.calls.append(time.time())                    # a failed call counts too (the hourly cap)
            text = self._ask(SYSTEM, prompt(h, key, d))
            if not text:
                raise RuntimeError("the model returned no summary")
            out = h.set_summary_ai(key, text, MODEL, sig)
        print(f"{datetime.now():%H:%M:%S} summary written for {d.get('name') or key} ({len(text)} chars)", flush=True)
        try:
            self.st.bus.publish("minutes", {"key": key})
        except Exception:  # noqa: BLE001
            pass
        return out

    def due(self, now: datetime) -> list[str]:
        """Nets over (closed, or quiet QUIET_MIN) in the last LOOK_BACK_H hours whose summary is missing or stale."""
        h = self.st.history
        since = (now - timedelta(hours=LOOK_BACK_H)).isoformat(timespec="seconds")
        quiet = (now - timedelta(minutes=QUIET_MIN)).isoformat(timespec="seconds")
        rows = h._q("SELECT key FROM inst WHERE kind='net' AND tx_count>=3 AND start>=? AND (end<=? OR closed_at IS NOT"
                    " NULL) ORDER BY start", (since, quiet))
        out = []
        for r in rows:
            cur = h.summary_ai(r["key"])
            if cur and cur["edited"]:
                continue
            try:
                if cur and cur["sig"] == h.summary_sig(r["key"]):
                    continue
            except LookupError:
                continue
            out.append(r["key"])
        return out

    def tick(self, now: datetime | None = None) -> int:
        if not enabled(self.st) or not self.st.costs.allow_background():   # off, no key, or over the AI budget
            return 0
        now = now or self.st.clock()
        self.calls = [t for t in self.calls if t > time.time() - 3600]
        n = 0
        for key in self.due(now):
            if len(self.calls) >= MAX_PER_HOUR:
                break
            try:
                self.write(key)
                n += 1
                self.last_error = None
            except Exception as e:  # noqa: BLE001 - offline, a bad key: the rule lines stand; tried again next minute
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"summary: {self.last_error}", flush=True)
                break
        return n

    def run(self, stop: threading.Event):
        while not stop.wait(EVERY_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                print(f"summary: {type(e).__name__}: {e}", flush=True)
