"""The station agent, DRAFT mode: it listens to a radio and writes, on screen, what it would say and when. It never
transmits (station/cat.py still refuses PTT; nothing here can key a radio).

Owner 2026-11-10: an AI agent associated with the station's call that checks in to nets and converses "just like a
HAM radio operator", with natural pauses; "We must ensure that all communications with [the agent] comply with FCC
guidelines." Draft mode comes first: it proves the timing and the words on real nets before anything is keyed, and
it lets models be compared on the same traffic (operator ratings + measured latency).

Every draft passes two layers:
  1. the model is told the rules (SYSTEM below), and
  2. FCC Part 97 checks in code (fcc_check), which the model cannot talk its way past. A draft that fails any check is
     kept and shown, marked "would not send", with the rule.
The checks (47 CFR Part 97, as read 2026-11-10):
  97.119(a)  identification: the station's call at the end of each contact and at least every 10 minutes during it;
             by voice in English (phonetics allowed). An SSID ("-20") is not a voice identifier: never added.
  97.113(a)(4)  no false or deceptive messages: the agent never claims to be a person, and on its first transmission
             in a contact discloses that it is an automated assistant with a control operator.
  97.113(a)(4)  no music, no obscene or indecent words, no codes or ciphers meant to obscure the meaning.
  97.113(a)(2),(3)  no business: nothing for pay, no selling, no ordering on anyone's behalf.
  97.113(b)  no broadcasting (one-way information to the public): the agent only answers or checks in.
  97.115     no third-party traffic: it never relays a message for someone else (the operator does that himself).
  97.101(c)/(d)  emergency traffic has priority and no willful interference: during emergency or priority traffic,
             or while someone is transmitting, the agent is silent.
  97.105 / 97.109(d)  a voice QSO station may not be under automatic control: a control operator must be at the
             control point. Draft mode transmits nothing; a later mode that transmits needs the operator present.
  97.301-305  the operator's license privileges: the frequency must be inside the band segment the operator's class
             may use for phone (license class: Settings -> Station agent).
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timedelta

from . import secrets

DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"     # measured 2026-11-10: 0.55 s; grok-4.3..4.7 took 3-11 s
MODELS = {
    "grok-4.20-0309-non-reasoning": {"label": "Grok 4.20 fast (xAI)", "provider": "xai"},
    "grok-4.5": {"label": "Grok 4.5 (xAI, reasoning: slower)", "provider": "xai"},
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5 (Anthropic)", "provider": "anthropic"},
}
CONTEXT_MIN = 20          # transcript the model sees
CONTEXT_MAX = 24          # at most this many transmissions
DEBOUNCE_S = 1.2          # a reply is drafted after the frequency has been quiet this long
ID_EVERY_MIN = 10         # 97.119(a)
MAX_WORDS = 75            # about 30 s of speech: the transmit cap in the design
PER_HOUR = 240            # model calls per hour, all radios (cost guard)
KEEP = 60                 # drafts kept in memory (all are appended to data/agent_drafts.jsonl)

PHONETIC = {"A": "Alfa", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf",
            "H": "Hotel", "I": "India", "J": "Juliett", "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November",
            "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango", "U": "Uniform",
            "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu", "0": "Zero", "1": "One",
            "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}

# US phone privileges (MHz), 97.301 / 97.305; 60 m is five USB channels for General and up.
PHONE = {
    "technician": [(28.300, 28.500), (50.100, 54.0), (144.100, 148.0), (222.0, 225.0), (420.0, 450.0)],
    "general": [(1.800, 2.000), (3.800, 4.000), (7.175, 7.300), (14.225, 14.350), (18.110, 18.168), (21.275, 21.450),
                (24.930, 24.990), (28.300, 29.700), (50.100, 54.0), (144.100, 148.0), (222.0, 225.0), (420.0, 450.0)],
    "extra": [(1.800, 2.000), (3.600, 4.000), (7.125, 7.300), (14.150, 14.350), (18.110, 18.168), (21.200, 21.450),
              (24.930, 24.990), (28.300, 29.700), (50.100, 54.0), (144.100, 148.0), (222.0, 225.0), (420.0, 450.0)],
}
PHONE["advanced"] = [(1.800, 2.000), (3.700, 4.000), (7.125, 7.300), (14.175, 14.350), (18.110, 18.168),
                     (21.225, 21.450), (24.930, 24.990), (28.300, 29.700), (50.100, 54.0), (144.100, 148.0),
                     (222.0, 225.0), (420.0, 450.0)]
SIXTY_M = (5.3305, 5.3465, 5.3570, 5.3715, 5.4035)    # channel centres (USB dial 1.5 kHz below)

KINDS = ["check_in", "reply", "acknowledge", "stand_by", "say_again", "sign_off", "none"]
SCHEMA = {
    "type": "object",
    "properties": {
        "speak": {"type": "boolean"},
        "kind": {"type": "string", "enum": KINDS},
        "say": {"type": "string"},
        "wait_s": {"type": "number"},
        "why": {"type": "string"},
    },
    "required": ["speak", "kind", "say", "wait_s", "why"],
    "additionalProperties": False,
}

SYSTEM = """You are {name}, the automated voice assistant of amateur radio station {call}. The licensee and control \
operator is {op}. You are in DRAFT mode: nothing you write is transmitted; the operator reads your drafts to judge you.

You listen to a frequency through speech recognition (it makes mistakes: calls may be misheard, words dropped). After \
each transmission decide whether {call} should transmit now, and if so exactly what to say, as an experienced, \
courteous ham operator would on voice.

Speak ONLY when:
- net control calls for check-ins of a kind that fits this station (and {call} has not checked in yet this net):
  check in briefly, using the check-in line if one is given;
- someone calls {call} or {name} directly, or asks this station a question: answer it, briefly;
- net control acknowledges or recognizes {call} and expects an answer;
- the copy was too poor to answer: ask them to say again (kind say_again);
- you need a moment to look something up: say "stand by one" (kind stand_by), then answer next time.
Otherwise stay silent (speak false, kind none). Never break into another conversation, never call CQ, never talk \
just to fill silence. During emergency or priority traffic stay silent unless net control asks this station directly.

Voice protocol: use the other station's call when answering; "over" only on HF when turn-taking is unclear; standard \
phonetics for calls and spelled words; plain language, no Q-codes except common ones (QSL, QRZ, QTH) used naturally; \
no jargon the other side might not know. Sound human-natural and brief (one to three sentences, under 70 words).

FCC Part 97 rules you must follow (the station's software also enforces them and will block a draft that breaks one):
- Identify with {call} at the end of each exchange and at least every ten minutes; never use an SSID like -20 on voice.
- Never pretend to be a person. On your first transmission in a contact or net, say you are {call}'s automated \
assistant and that {op} is control operator.
- No music, no obscene or indecent language, no codes or ciphers that hide meaning.
- No business: never buy, sell, advertise or arrange anything for pay; no messages for anyone's employer.
- No broadcasting to the public; no relaying messages on someone else's behalf (third-party traffic) - tell them \
{op} will handle it personally.
- Never give legal, medical or emergency-response instructions as fact; for an emergency, say {op} is being alerted.
- If you are unsure whether something is allowed, stay silent and explain why in "why".

Answer with JSON only: {{"speak": bool, "kind": one of {kinds}, "say": "the exact words, or empty", \
"wait_s": seconds to wait after the frequency goes quiet (1.5-2.5 normally, 0.8 for a quick acknowledgement), \
"why": "one short line for the operator"}}."""


def phonetic(call: str) -> str:
    return " ".join(PHONETIC.get(c, c) for c in call.upper() if c.isalnum())


def _has_call(text: str, call: str) -> bool:
    if not call:
        return True
    t = text.upper()
    if re.search(r"\b" + re.escape(call.upper()) + r"\b", t):
        return True
    return phonetic(call).upper() in re.sub(r"\s+", " ", t)


# ---------------------------------------------------------------- FCC checks (code, not the model) ---------------
_OBSCENE = re.compile(r"\b(fuck\w*|shit\w*|cunt|bitch\w*|asshole|motherf\w*|cock\b|dick\b|pussy|bastard)", re.I)
_MUSIC = re.compile(r"(♪|♫|\bla la la\b|\b(sing|singing|hum|humming|plays? (a |the )?(song|tune|music))\b)", re.I)
_BUSINESS = re.compile(r"\b(for sale|sell(ing)?|buy(ing)?|price|discount|order (it|one|now)|\$\d|dollars|"
                       r"advertis\w*|sponsor\w*|my (employer|company|business))\b", re.I)
_THIRD = re.compile(r"\b(relay(ing)? (a|this|the) (message|traffic)|pass(ing)? (a|this) message|on behalf of|"
                    r"tell \w+ that|message for (him|her|them|\w+ \w+))\b", re.I)
_PERSON = re.compile(r"\b(i am|i'm|this is) (a )?(human|person|real person|the operator|the licensee)\b", re.I)
_CODE = re.compile(r"\b[A-Z0-9]{5}(\s+[A-Z0-9]{5}){2,}\b")      # five-letter groups: a cipher


def privileges(freq_mhz, mode, license_class) -> tuple[bool | None, str]:
    """(ok, why). None = can't tell (no frequency or no class set)."""
    if not freq_mhz:
        return None, "no frequency known"
    if not license_class:
        return None, "the operator's license class is not set (Settings → Station agent)"
    f = float(freq_mhz)
    if 5.3 <= f <= 5.41:
        if license_class == "technician":
            return False, "60 m is not a Technician phone band"
        if any(abs(f - c) <= 0.0016 or abs(f - (c - 0.0015)) <= 0.0002 for c in SIXTY_M):
            return (mode or "USB").upper() == "USB" or None, "60 m channel (USB only)"
        return False, f"{f:.4f} MHz is not one of the five 60 m channels"
    seg = PHONE.get(license_class, [])
    if any(a <= f <= b for a, b in seg):
        return True, f"{f:.3f} MHz is inside {license_class.title()} phone privileges"
    return False, f"{f:.3f} MHz is outside {license_class.title()} phone privileges (97.301/97.305)"


def fcc_check(d: dict, ctx: dict) -> list[dict]:
    """Every rule, pass or fail, for one draft. ctx: call, name, op, first (first transmission in this contact),
    id_due (10 minutes since the last ID), emergency (priority traffic on now), busy (someone transmitting),
    freq_mhz, mode, license_class."""
    say = (d.get("say") or "").strip()
    out = []

    def rule(ref, ok, note):
        out.append({"rule": ref, "ok": ok, "note": note})
    if not d.get("speak") or not say:
        rule("97.101", True, "silent")
        return out
    words = len(say.split())
    rule("97.119(a)", _has_call(say, ctx["call"]) if (ctx["id_due"] or d.get("kind") in ("sign_off", "check_in", "reply"))
         else True, f"identifies as {ctx['call']}" if _has_call(say, ctx["call"]) else
         f"must say {ctx['call']} ({'10 minutes since the last ID' if ctx['id_due'] else 'end of an exchange'})")
    disclosed = re.search(r"\bautomat\w*\b", say, re.I) is not None
    rule("97.113(a)(4) disclosure", disclosed or not ctx["first"],
         "says it is automated" if disclosed else
         "first transmission must say it is an automated assistant" if ctx["first"] else "disclosed earlier")
    me = ctx.get("op") if ctx.get("op") and ctx.get("op") != "the operator" else None
    poses = bool(_PERSON.search(say)) or bool(me and re.search(r"\b(i am|i'm|my name is|this is) " + re.escape(me) + r"\b",
                                                             say, re.I))
    rule("97.113(a)(4) not a person", not poses, "never claims to be a person" if not poses else
         "claims to be a person (or to be the operator)")
    rule("97.113(a)(4) language", not _OBSCENE.search(say), "clean" if not _OBSCENE.search(say) else "indecent word")
    rule("97.113(a)(4) music", not _MUSIC.search(say), "no music" if not _MUSIC.search(say) else "music")
    rule("97.113(a)(4) codes", not _CODE.search(say), "plain language" if not _CODE.search(say) else
         "groups that look like a cipher")
    rule("97.113(a)(2),(3) business", not _BUSINESS.search(say), "no business" if not _BUSINESS.search(say) else
         "sounds like business or pecuniary interest")
    rule("97.115 third party", not _THIRD.search(say), "no relayed messages" if not _THIRD.search(say) else
         "relays a message for someone else: the operator handles third-party traffic")
    rule("97.101(c) emergency priority", not ctx["emergency"] or d.get("addressed"),
         "no priority traffic on" if not ctx["emergency"] else
         "net control called this station" if d.get("addressed") else "emergency/priority traffic: stay silent")
    rule("97.101(d) interference", not ctx["busy"], "the frequency is clear" if not ctx["busy"] else
         "someone is transmitting")
    rule("transmit cap", words <= MAX_WORDS, f"{words} words (about {words * 0.4:.0f} s)")
    ok, why = privileges(ctx.get("freq_mhz"), ctx.get("mode"), ctx.get("license_class"))
    rule("97.301/97.305 privileges", ok is not False, why)
    rule("97.105/97.109(d) control operator", True, "Draft mode: nothing is transmitted")
    return out


# ---------------------------------------------------------------- providers --------------------------------------
def call_xai(key, model, system, user, timeout=20):
    body = {"model": model, "max_tokens": 300, "messages": [{"role": "system", "content": system},
                                                           {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "draft", "schema": SCHEMA,
                                                                       "strict": True}}}
    req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return json.loads(d["choices"][0]["message"]["content"]), d.get("usage") or {}


def call_anthropic(key, model, system, user, timeout=20):
    import anthropic                                   # install/windows/requirements-windows.txt
    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)
    resp = client.messages.create(model=model, max_tokens=1000, system=system,
                                  messages=[{"role": "user", "content": user}],
                                  output_config={"effort": "low",
                                                 "format": {"type": "json_schema", "schema": SCHEMA}})
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}


PROVIDERS = {"xai": ("XAI_API_KEY", call_xai), "anthropic": ("ANTHROPIC_API_KEY", call_anthropic)}


# ---------------------------------------------------------------- the agent --------------------------------------
class Agent:
    """Drafts per radio. Settings in data/agent.json (never committed): name, check-in line, operator's first name,
    license class, model, compare models, and each radio's mode ("off" | "draft")."""

    def __init__(self, station, providers=None):
        self.st = station
        self.providers = providers or PROVIDERS
        self.path = station.data / "agent.json"
        self.log_path = station.data / "agent_drafts.jsonl"
        self.lock = threading.Lock()
        self.cfg = {"name": "Echo", "checkin": "", "operator": "", "license_class": "", "model": DEFAULT_MODEL,
                    "compare": [], "radios": {}}
        try:
            self.cfg.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.drafts: deque = deque(maxlen=KEEP)
        self.calls: deque = deque()                 # monotonic times of model calls (rate guard)
        self.timers: dict[str, threading.Timer] = {}
        self.state: dict[str, dict] = {}            # radio -> {"last_id": datetime, "disclosed": key, "checked_in": key}

    # ----- settings -----
    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def settings(self, body: dict) -> dict:
        with self.lock:
            for k in ("name", "checkin", "operator"):
                if k in body:
                    v = re.sub(r"\s+", " ", str(body[k] or "")).strip()[:120 if k == "checkin" else 24]
                    if k == "name" and not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,23}", v):
                        raise ValueError("the name is letters only (it is spoken on the air)")
                    self.cfg[k] = v
            if "license_class" in body:
                v = str(body["license_class"] or "").lower()
                if v and v not in PHONE:
                    raise ValueError("license class: technician, general, advanced or extra")
                self.cfg["license_class"] = v
            if "model" in body:
                if body["model"] not in MODELS:
                    raise ValueError("unknown model")
                self.cfg["model"] = body["model"]
            if "compare" in body:
                c = [m for m in (body["compare"] or []) if m in MODELS and m != self.cfg["model"]][:2]
                self.cfg["compare"] = c
            self._save()
        return self.view()

    def set_radio(self, rid: str, mode: str) -> dict:
        if rid not in self.st.radios:
            raise ValueError(f"no radio called {rid!r}")
        if mode not in ("off", "draft"):
            raise ValueError("mode: off or draft (nothing else exists yet: the agent never transmits)")
        with self.lock:
            self.cfg.setdefault("radios", {})[rid] = mode
            self._save()
        return self.view()

    def mode(self, rid) -> str:
        return (self.cfg.get("radios") or {}).get(rid, "off")

    def view(self) -> dict:
        keys = {p: secrets.status(self.st.root, k)["set"] for p, (k, _) in self.providers.items()}
        return {"settings": {k: v for k, v in self.cfg.items()}, "call": self.st.me,
                "phonetic": phonetic(self.st.me or ""),
                "models": [{"id": m, "label": v["label"], "ready": keys.get(v["provider"], False)}
                           for m, v in MODELS.items()],
                "classes": list(PHONE), "drafts": list(self.drafts)}

    # ----- hearing -----
    def on_final(self, tx: dict):
        """A transmission was transcribed: draft (after DEBOUNCE_S of quiet) on a radio in Draft mode."""
        rid = tx.get("radio")
        if self.mode(rid) != "draft" or tx.get("noise") or not (tx.get("text") or "").strip():
            return
        old = self.timers.pop(rid, None)
        if old:
            old.cancel()
        t = threading.Timer(DEBOUNCE_S, self._safe_draft, args=(rid, tx.get("id")))
        t.daemon = True
        self.timers[rid] = t
        t.start()

    def _safe_draft(self, rid, after):
        try:
            self.draft(rid, after)
        except Exception as e:  # noqa: BLE001 - the agent never stops the station
            print(f"agent {rid}: {type(e).__name__}: {e}", flush=True)

    def _context(self, rid, now):
        since = (now - timedelta(minutes=CONTEXT_MIN)).isoformat(timespec="seconds")
        rows = [t for t in list(self.st.tx) if t.get("radio") == rid and t.get("status") == "final"
                and str(t.get("start") or "") >= since and not t.get("noise")][-CONTEXT_MAX:]
        return rows

    def _net(self, rid, now):
        try:
            return (self.st._current_by_radio(now) or {}).get(rid) or {}
        except Exception:  # noqa: BLE001
            return {}

    def draft(self, rid, after=None, asked=False) -> dict | None:
        now = self.st.clock()
        if not asked:
            mono = time.monotonic()
            while self.calls and mono - self.calls[0] > 3600:
                self.calls.popleft()
            if len(self.calls) >= PER_HOUR:
                return None
            self.calls.append(mono)
        rows = self._context(rid, now)
        if not rows and not asked:
            return None
        r = self.st.radios.get(rid) or {}
        net = self._net(rid, now)
        call, name = self.st.me or "N0CALL", self.cfg.get("name") or "Echo"
        op = self.cfg.get("operator") or "the operator"
        st = self.state.setdefault(rid, {})
        contact = (net.get("key") or f"{rid}@{now:%Y-%m-%d}")
        last_id = st.get("last_id")
        id_due = not last_id or now - last_id >= timedelta(minutes=ID_EVERY_MIN)
        first = st.get("disclosed") != contact
        emergency = bool(net.get("emergency")) and bool(re.search(r"\b(emergency|priority|break break)\b",
                                                                  " ".join(t.get("text", "") for t in rows[-3:]), re.I))
        busy = bool(r.get("carrier"))                      # a signal on the frequency right now
        lines = []
        for t in rows:
            who = t.get("speaker") or (t.get("callsigns") or ["?"])[0]
            lines.append(f"[{str(t.get('start', ''))[11:19]}] {who}: {t.get('text', '').strip()}")
        user = "\n".join([
            f"Frequency: {r.get('freq_mhz') or '?'} MHz {r.get('mode') or ''}",
            f"Net: {net.get('name') or 'none scheduled now'}" + (f", net control {net['ncs']}" if net.get("ncs") else ""),
            f"Check-in line (the operator's own words, adapt them for yourself): {self.cfg.get('checkin') or '(none)'}",
            f"{call} has {'already' if st.get('checked_in') == contact else 'not yet'} checked in to this net.",
            f"Your first transmission in this contact: {'yes - disclose' if first else 'no'}.",
            f"Minutes since {call} last identified: {'never' if not last_id else int((now - last_id).total_seconds() // 60)}.",
            "Transcript (oldest first; speech recognition, may contain errors):",
            *(lines or ["(nothing heard yet)"]),
            "Should " + call + " transmit now? Answer with the JSON.",
        ])
        system = SYSTEM.format(name=name, call=call, op=op, kinds="/".join(KINDS))
        addressed = bool(rows) and (call in (rows[-1].get("callsigns") or [])
                                    or re.search(r"\b" + re.escape(name) + r"\b", rows[-1].get("text", ""), re.I))
        ctx = {"call": call, "name": name, "op": op, "first": first, "id_due": id_due, "emergency": emergency,
               "busy": busy, "freq_mhz": r.get("freq_mhz"), "mode": r.get("mode"),
               "license_class": self.cfg.get("license_class")}
        models = [self.cfg.get("model") or DEFAULT_MODEL] + list(self.cfg.get("compare") or [])
        results = {}

        def run(m):
            results[m] = self._ask(m, system, user, ctx, addressed)
        threads = [threading.Thread(target=run, args=(m,), daemon=True) for m in models]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        main = results.get(models[0]) or {"error": "no answer"}
        d = {"id": uuid.uuid4().hex[:10], "radio": rid, "at": now.isoformat(timespec="seconds"), "after": after,
             "heard": lines[-1] if lines else "", "net": net.get("name"), "addressed": bool(addressed),
             "asked": asked, **main, "alts": [results[m] | {"model": m} for m in models[1:] if m in results]}
        if d.get("speak") and d.get("ok"):
            if _has_call(d.get("say", ""), call):
                st["last_id"] = now                              # as if sent: Draft mode keeps the clock honest
            if re.search(r"\bautomat\w*\b", d.get("say", ""), re.I):
                st["disclosed"] = contact
            if d.get("kind") == "check_in":
                st["checked_in"] = contact
        with self.lock:
            self.drafts.append(d)
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(d) + "\n")
            except OSError:
                pass
        self.st.bus.publish("agent", {"draft": d})
        return d

    def _ask(self, model, system, user, ctx, addressed) -> dict:
        prov = MODELS[model]["provider"]
        keyname, fn = self.providers[prov]
        key = secrets.read(self.st.root, keyname)
        if not key:
            return {"model": model, "error": f"no {keyname} set (Settings → Station agent)"}
        t = time.monotonic()
        try:
            out, usage = fn(key, model, system, user)
        except urllib.error.HTTPError as e:
            return {"model": model, "error": f"{prov} answered {e.code}", "latency_s": round(time.monotonic() - t, 2)}
        except Exception as e:  # noqa: BLE001
            return {"model": model, "error": f"{type(e).__name__}: {str(e)[:120]}",
                    "latency_s": round(time.monotonic() - t, 2)}
        lat = round(time.monotonic() - t, 2)
        d = {"model": model, "latency_s": lat, "usage": usage, "speak": bool(out.get("speak")),
             "kind": out.get("kind") if out.get("kind") in KINDS else "none", "say": str(out.get("say") or "")[:600],
             "wait_s": max(0.5, min(4.0, float(out.get("wait_s") or 2.0))), "why": str(out.get("why") or "")[:200],
             "addressed": bool(addressed)}
        d["fcc"] = fcc_check(d, ctx)
        d["ok"] = all(c["ok"] for c in d["fcc"])
        # the courtesy gap hides the model: it starts when the frequency went quiet, so the model time counts
        d["silence_s"] = round(max(d["wait_s"], DEBOUNCE_S + lat), 1)
        return d

    def rate(self, did: str, good: bool | None, note: str = "") -> dict:
        with self.lock:
            for d in self.drafts:
                if d["id"] == did:
                    d["rating"] = {"good": good, "note": str(note or "")[:200]}
                    try:
                        with open(self.log_path, "a", encoding="utf-8") as fh:
                            fh.write(json.dumps({"rate": did, "good": good, "note": note}) + "\n")
                    except OSError:
                        pass
                    break
            else:
                raise ValueError("no such draft")
        return self.view()

    def post(self, path: str, body: dict) -> dict:
        """POST /api/agent/settings, /api/agent/radio/<rid> {"mode"}, /api/agent/ask/<rid> (draft now),
        /api/agent/rate/<draft id> {"good", "note"}."""
        m = re.fullmatch(r"/api/agent/(settings|radio|ask|rate)(?:/([\w-]+))?", path)
        if not m:
            raise ValueError("unknown agent request")
        what, arg = m.groups()
        if what == "settings":
            return self.settings(body)
        if what == "radio":
            return self.set_radio(arg, str(body.get("mode") or "off"))
        if what == "ask":
            if arg not in self.st.radios:
                raise ValueError(f"no radio called {arg!r}")
            self.draft(arg, asked=True)
            return self.view()
        return self.rate(arg, body.get("good"), body.get("note", ""))
