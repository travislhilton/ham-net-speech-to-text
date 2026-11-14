"""The operator's side chat with Grok, beside a radio's log: ask anything, and have it write words to say.

Owner 2026-11-12: "integrate an interactive AI chat that the operator can use to interact with GROK and then ask GROK
to prepare something to say. It should be informed by all things HAM operators voice protocol and constraints … review
the response and then save it, or put it into a prepared list that can be selected during a net or conversation to
transmit … it should have context of the chat or net and all the operators participating … the frequency/band …
previous recordings of the same net … smart about HAM radio … side chats asking for any kind of information." And:
"the operator should be able to play back any saved AI responses on the computer to hear them before they are
transmitted."

* The chat is per radio, kept in data/chat/<radio>.jsonl (never committed). It is the OPERATOR's: nothing in it is
  transmitted. A reply can carry words ready for the air ("say"); the operator hears them (▶, in the browser only),
  edits them and saves them to the prepared list (station/topics.py), where they are approved, armed or sent.
* Every turn gives the model: the station (call, operator, license class and its phone privileges, from the code),
  the radio (frequency, band, mode, the privileges there), the net or conversation on now (name, net control, the
  check-ins with names and towns, traffic, stations heard), the last transmissions heard (speech recognition), the
  earlier sessions of the same net (who usually checks in, the last minutes), the library's passages for the question
  (station/knowledge.py) and, when two of the operator's sites are named, the computed terrain path.
* Words for the air are checked in code like every Sidekick draft (station/agent.py fcc_check): ID, no music, codes,
  business or relayed messages, inside privileges, one transmission long.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HISTORY_TURNS = 12          # chat turns given back to the model
TRANSCRIPT_LINES = 40       # the last transmissions heard on the radio
CHAT_TIMEOUT_S = 90

CHAT_SYSTEM = """You are {name}, the AI assistant at amateur radio station {call}; you are talking with the station's \
licensee and control operator, {op}, in a side chat on the station's screen. Nothing you write here is transmitted. \
You are expert in amateur radio: FCC Part 97 (identification 97.119, prohibited content 97.113, third-party traffic \
97.115, emergency priority 97.101, privileges 97.301/97.305, control operators 97.105/97.109), voice procedure \
(ITU phonetics, prowords: over, out, roger, wilco, say again, break, clear, stand by; directed and open nets, net \
control's role, check-in order, traffic handling and precedence, NTS/ARES/RACES practice, ICS forms 205/213/214/309), \
repeaters and simplex, HF/VHF/UHF propagation, antennas and feed lines, digital modes and Winlink (VARA FM/HF, packet, \
RMS gateways, peer to peer), emergency communications and served agencies.

Answer the operator's question directly and practically. Use the station's facts below over your memory (the \
privileges and computed path results are authoritative; the transcript is speech recognition and may contain \
errors; say when you are unsure). Be concise unless asked for depth.

When the operator asks for something to SAY on the air (a check-in, a reply, an announcement, a short explanation, \
an answer to someone on the net), also write it in "say": spoken words for one transmission ({words} words at most), \
plain spoken English, standard phonetics for call signs where helpful, no lists, no URLs, no abbreviations a \
listener might not know, ending with the station's call sign {call}. If the operator will be the one speaking, write \
it in {op}'s voice; if the assistant will speak it on the air, it begins "This is {call}, {name}, an automated \
assistant; {op} is control operator" the first time. Fit the moment: in a directed net, words for when net control \
gives the floor. Never write music, codes or ciphers, business, or a message relayed for someone else. Otherwise \
leave "say" empty. "title" is a short name for the words (for the prepared list).
Return JSON: reply (your answer to the operator), say (words for the air, or ""), title."""

CHAT_SCHEMA = {"type": "object",
               "properties": {"reply": {"type": "string"}, "say": {"type": "string"}, "title": {"type": "string"}},
               "required": ["reply", "say", "title"], "additionalProperties": False}


class Chat:
    def __init__(self, agent):
        self.agent, self.st = agent, agent.st
        self.dir = Path(self.st.data) / "chat"
        self.lock = threading.Lock()
        self.busy: dict[str, bool] = {}

    def _path(self, rid: str) -> Path:
        if not re.fullmatch(r"[\w-]{1,40}", rid or ""):
            raise ValueError("bad radio")
        return self.dir / f"{rid}.jsonl"

    def thread(self, rid: str, limit: int = 60) -> list[dict]:
        try:
            lines = self._path(rid).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def clear(self, rid: str):
        p = self._path(rid)
        if p.exists():
            p.rename(p.with_name(f"{rid}-{datetime.now():%Y%m%d-%H%M%S}.jsonl.old"))

    def _add(self, rid: str, msg: dict) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.lock, open(self._path(rid), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg) + "\n")
        try:
            self.st.bus.publish("agent", {"chat": {"radio": rid, **msg}})
        except Exception:  # noqa: BLE001
            pass
        return msg

    # ----- what the model is told -----
    def context(self, rid: str, question: str, tools: bool = False) -> list[str]:
        from .agent import facts, privileges
        a, st = self.agent, self.st
        now = st.clock()
        r = st.radios.get(rid) or {}
        cls = a.cfg.get("license_class")
        ok, why = privileges(r.get("freq_mhz"), r.get("mode"), cls)
        out = [*facts(),
               f"This station: {st.me or 'N0CALL'}, control operator {a.cfg.get('operator') or 'the operator'}, "
               f"license class {cls or 'not set'}; {a.cfg.get('station') or 'a fixed home station'}.",
               f"The radio: {r.get('name') or rid}, {r.get('freq_mhz') or '?'} MHz {r.get('mode') or ''} "
               f"({r.get('band') or '?'}); privileges there: {why or ('yes' if ok else 'unknown')}.",
               f"Local time {now:%A %Y-%m-%d %H:%M}."]
        try:
            b = st.history.board(rid, now)
        except Exception:  # noqa: BLE001
            b = None
        if b:
            roll = "; ".join(f"{x.get('call')}" + (f" {x['name']}" if x.get("name") else "")
                             + (f" ({x['city']})" if x.get("city") else "") + (" [traffic]" if x.get("traffic") else "")
                             + ("" if x.get("confirmed") else " [heard once: unconfirmed]") for x in (b.get("roll") or [])[:60])
            out += [f"On the air now: {b.get('name') or 'a conversation'} ({b.get('kind')}), "
                    f"net control {b.get('ncs') or 'unknown'}, started {str(b.get('start') or '')[11:16]}"
                    + (f", closed {str(b['closed_at'])[11:16]}" if b.get("closed_at") else "") + ".",
                    f"Taking part ({len(b.get('roll') or [])}): {roll or 'none heard yet'}."]
            if b.get("mentioned"):
                out.append("Heard but not checked in: " + ", ".join(map(str, b["mentioned"][:20])))
            if b.get("missing"):
                out.append("Regulars not heard yet: " + ", ".join(map(str, b["missing"][:20])))
            out += self._earlier(b)
        rows = [t for t in list(st.tx) if t.get("radio") == rid and t.get("status") == "final" and not t.get("noise")
                and str(t.get("start") or "") >= (now - timedelta(minutes=90)).isoformat()][-TRANSCRIPT_LINES:]
        if rows:
            out.append("Transmissions heard on this radio (oldest first; speech recognition, may contain errors):")
            for t in rows:
                who = t.get("speaker") or (t.get("callsigns") or ["?"])[0]
                out.append(f"[{str(t.get('start', ''))[11:19]}] {who}: {(t.get('text') or '').strip()}")
        if tools:                                               # xAI chat: it has the tools instead
            return out
        try:
            lib = a.library().context_for(question, 5000)
            if lib:
                out += ["Reference material from the station's library (the operator's documents and this station's "
                        "manual):", lib]
        except Exception:  # noqa: BLE001
            pass
        try:
            from .topics import path_facts, sites_named
            named = sites_named(question, a.topics.sites)
            if len(named) >= 2:
                f = float(r.get("freq_mhz") or 146.0)
                out += path_facts(named[:4], f if 30 <= f <= 1300 else 146.0)
            elif a.topics.sites:
                out.append("The operator's sites (name them to have a path computed): "
                           + "; ".join(s["name"] + (f" ({s['note']})" if s.get("note") else "") for s in a.topics.sites))
        except Exception:  # noqa: BLE001
            pass
        return out

    def _earlier(self, b: dict) -> list[str]:
        """The same net's earlier sessions: who usually checks in, and the last session's minutes."""
        h = self.st.history
        net_id = (b.get("key") or "").split("@")[0] if b.get("kind") == "net" else None
        if not net_id:
            return []
        out = []
        try:
            an = h.analysis(net_id)
            reg = an.get("regulars") or []
            if reg:
                out.append(f"This net's history: {len(an.get('attendance') or [])} sessions heard; regulars: "
                           + ", ".join(map(str, reg[:25])) + ".")
        except Exception:  # noqa: BLE001
            pass
        try:
            prev = [i for i in h.instances(net=net_id, kind="net", limit=3) if i.get("key") != b.get("key")]
            if prev:
                txt = h.minutes_text(prev[0]["key"], md=False)
                out.append(f"Minutes of the last session ({str(prev[0].get('start') or '')[:10]}):\n" + txt[:2500])
        except Exception:  # noqa: BLE001
            pass
        return out

    # ----- a turn -----
    def ask(self, rid: str, message: str, deep: bool = False, wait: bool = False) -> dict:
        message = str(message or "").strip()[:4000]
        if not message:
            raise ValueError("type a question")
        if rid not in self.st.radios:
            raise ValueError("no such radio")
        if self.busy.get(rid):
            raise ValueError("still answering the last question")
        a = self.agent
        from .agent import DEEP_MODEL, DEFAULT_MODEL, MODELS, secrets
        model = (a.cfg.get("deep_model") or DEEP_MODEL) if deep else (a.cfg.get("model") or DEFAULT_MODEL)
        prov = MODELS.get(model, MODELS[DEFAULT_MODEL])["provider"]
        keyname, fn = a.providers[prov]
        key = secrets.read(self.st.root, keyname)
        if not key:
            raise ValueError(f"the chat needs the {keyname} (Settings → Station agent)")
        q = self._add(rid, {"id": uuid.uuid4().hex[:8], "role": "operator", "text": message,
                            "at": datetime.now().isoformat(timespec="seconds")})
        self.busy[rid] = True

        def run():
            t0 = datetime.now()
            try:
                from .agent import ANSWER_WORDS, TOOLS_NOTE, call_xai, call_xai_tools
                system = CHAT_SYSTEM.format(name=a.name, call=self.st.me or "N0CALL",
                                            op=a.cfg.get("operator") or "the operator", words=ANSWER_WORDS)
                past = [m for m in self.thread(rid, HISTORY_TURNS * 2 + 1) if m["id"] != q["id"]][-HISTORY_TURNS * 2:]
                user = "\n".join([*self.context(rid, message, fn is call_xai), "", "The chat so far:",
                                  *[f"{'Operator' if m['role'] == 'operator' else 'You'}: {m.get('text', '')}"
                                    + (f"\n  (words for the air: {m['say']})" if m.get("say") else "") for m in past],
                                  "", f"Operator: {message}"])
                if fn is call_xai:                              # xAI: the model asks for the library / a path itself
                    from .toolbox import Toolbox
                    out, usage = call_xai_tools(key, model, system + TOOLS_NOTE, user, Toolbox(a, rid),
                                                timeout=CHAT_TIMEOUT_S, schema=CHAT_SCHEMA,
                                                max_tokens=6000 if deep else 2500, name="chat")
                else:
                    out, usage = fn(key, model, system, user, timeout=CHAT_TIMEOUT_S, schema=CHAT_SCHEMA,
                                    max_tokens=6000 if deep else 2500, name="chat")
                msg = {"id": uuid.uuid4().hex[:8], "role": "assistant", "text": str(out.get("reply") or "").strip(),
                       "say": str(out.get("say") or "").strip(), "title": str(out.get("title") or "")[:80],
                       "model": model, "took_s": round((datetime.now() - t0).total_seconds(), 1),
                       "at": datetime.now().isoformat(timespec="seconds")}
                used = list(dict.fromkeys(t for t in (usage or {}).get("tools") or [] if t))
                if used:                                        # which tools the answer used, in first-call order
                    msg["tools"] = used
                if msg["say"]:
                    msg["check"] = self.check(rid, msg["say"])
            except Exception as e:  # noqa: BLE001
                msg = {"id": uuid.uuid4().hex[:8], "role": "assistant", "error": f"{type(e).__name__}: {str(e)[:200]}",
                       "text": "", "at": datetime.now().isoformat(timespec="seconds")}
            finally:
                self.busy[rid] = False
            self._add(rid, msg)
        th = threading.Thread(target=run, daemon=True, name=f"chat-{rid}")
        th.start()
        self._thread = th
        if wait:
            th.join(CHAT_TIMEOUT_S + 5)
        return {"asked": q}

    def check(self, rid: str, say: str) -> list[dict]:
        """The Part 97 content checks on words for the air (the same as every Sidekick draft)."""
        from .agent import fcc_check
        a = self.agent
        r = self.st.radios.get(rid) or {}
        ctx = {"call": self.st.me or "N0CALL", "name": a.name, "op": a.cfg.get("operator") or "the operator",
               "first": False, "id_due": True, "emergency": False, "busy": False, "freq_mhz": r.get("freq_mhz"),
               "mode": r.get("mode"), "license_class": a.cfg.get("license_class")}
        return fcc_check({"speak": True, "kind": "reply", "say": say}, ctx)
