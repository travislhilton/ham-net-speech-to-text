"""The end of a net: net control closing it on the air, or the operator pressing End net.

Hearing the close (pure, `detect`): the closing words, in their present and intent forms ("we'll go ahead and close
the net", "this net is now closed", "closing the net", "secure the net", "this concludes tonight's net", "return the
repeater to normal amateur use"), sentence by sentence. Not a close: a question ("shall we close the net?"),
planning ("before we close the net, any…", "we'll close the net at 9", "the net will close in 5 minutes" = a
"closing soon" hint), a negation, a quote, "the net is closed to check-ins". "QRT" and "<net control>, clear" count
only from net control.

Who may close it (`accept`): net control (the schedule's `ncs`, else the station that identified itself while opening
the net or taking roll: `learn_ncs`). When nobody can say who is talking (most transmissions on FM: net control
rarely gives its call when closing), the words alone count when they are net control's own closing words, the net
has run 5 minutes, and (a scheduled net) it is inside its window (start to end + 5 min grace).

What happens (NetCloser, owned by the Station):
  heard a close   the net is marked closed at that transmission (history: closed_at/closed_by; the log's header),
                  recording goes on `close_tail_min` (2) more minutes for the 73s and IDs, then:
                  - the radio is paused (by "schedule") when the station resumed it for this net, or it listens only
                    during nets, and the operator has not pressed Pause/Resume since: a radio the operator had
                    listening before the net is never paused; an unscheduled conversation never pauses anything;
                  - the FINAL accurate pass for the net starts at once (not after final_after_min quiet minutes);
                  - the minutes draft is refreshed; transmissions after the tail start a new conversation.
  End net         the operator ends it now: no tail, the radio is paused by the operator, the final pass starts.
  Not closed      undoes either: the radio is resumed if this paused it, the net runs on to its scheduled end, and
                  the final pass runs again later as usual.
Without a close, nothing changes: a scheduled net ends at its scheduled end + 5 minutes grace, as before.
State is kept in data/netclose.json (a restart inside the window does not resume a closed net's radio).
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta

from . import ics, speaker

MIN_NET_MIN = 5             # a close by wording alone needs this many minutes of net first
LEARN_MIN = 20              # net control is learned from the first this-many minutes of a net
KEEP_H = 36                 # closes older than this are forgotten

_CALL = r"[A-Z0-9]{1,3}\d[A-Z0-9/-]{1,8}"
_NETW = r"(?:net|nets|nett|nut)"                    # "net" and the ways Whisper writes it at a sentence end
_DET = r"(?:the|this|our|that|tonight'?s|today'?s|this\s+(?:evening|morning|afternoon)'?s|a)\s+"
_NAME = r"(?:[\w'.&-]+\s+){0,5}?"                 # "the Springfield County ARES net"

STRONG = [
    # close / secure / adjourn the net
    re.compile(r"\b(?:close|closing|closed|secure|securing|secured|adjourn|adjourning|terminate)\s+"
               r"(?:down\s+|out\s+|up\s+)?" + _DET + _NAME + _NETW + r"\b", re.I),
    # we'll end the net; this is the end of the net
    re.compile(r"\b(?:we'?ll|we\s+will|i'?ll|i\s+will|let'?s|going\s+to|gonna|now|and)\s+(?:go\s+ahead\s+and\s+)?"
               r"end\s+" + _DET + _NAME + _NETW + r"\b|\b(?:this|that)(?:'s|\s+is|\s+will\s+be)\s+(?:the\s+)?end\s+of\s+"
               + _DET + _NAME + _NETW + r"\b", re.I),
    # the net is (now) closed / secured
    re.compile(r"\b" + _NETW + r"(?:'s|\s+is|\s+has\s+been|\s+will\s+now\s+be|\s+now\s+stands)?\s+(?:now\s+|officially\s+|"
               r"hereby\s+)?(?:closed|secured|adjourned|concluded|qrt)\b", re.I),
    # this concludes tonight's net
    re.compile(r"\b(?:this|that)\s+(?:concludes|ends|completes|closes)\s+" + _DET + _NAME + r"(?:net|nets|session)\b",
               re.I),
    # return the repeater / frequency to normal (amateur) use
    re.compile(r"\b(?:return|returning|returns|turn|turning|release|releasing)\s+(?:the|this|our)\s+"
               r"(?:repeater|frequency|machine|channel|system)\s+(?:back\s+)?(?:over\s+)?to\s+(?:its\s+)?"
               r"(?:normal|regular|general|routine|open)\b", re.I),
]
# Net control's goodbye at the end of a net (owner 2026-11-03: a net ended with "Thank you all very much. Thank you
# for your patience. I want you all to have a safe and wonderful day" and no closing words). Counts only near the
# scheduled end (or while running over), after FAREWELL_MIN_NET minutes, from net control or addressed to everyone.
FAREWELL = re.compile(
    r"\bthank(?:s|\s+you)\s+(?:(?:all|everyone|everybody|y'?all|you\s+all|guys|folks)\s+)?(?:(?:very|so)\s+much\s+)?"
    r"for\s+(?:your\s+|the\s+|all\s+)?(?:participation|participating|patience|checking\s+in|check-?ins|joining|coming|"
    r"being\s+(?:here|on|with\s+us)|listening|support|time)\b"
    r"|\b(?:have|enjoy)\s+a\s+(?:\w+\s+){0,3}?(?:safe|good|great|wonderful|nice|pleasant|blessed|fantastic)\s+"
    r"(?:\w+\s+){0,2}?(?:evening|night|day|week|weekend|rest\s+of\s+(?:the|your)\s+(?:evening|night|day|week))\b(?!\s+(?:of|at|on|for)\b)"
    r"|\bgood\s?night,?\s+(?:everyone|everybody|all|y'?all|folks)\b|\b73,?\s+(?:to\s+)?(?:everyone|everybody|all|y'?all)\b",
    re.I)
EVERYONE = re.compile(r"\b(?:all|everyone|everybody|y'?all|you\s+all|folks|guys|participation|participating)\b", re.I)
_FUTURE_BEFORE = re.compile(r"\b(?:we|i|they|you|he|she|it)(?:'ll|\s+will|\s+would|'d|\s+are\s+going\s+to|\s+plan\s+to)\s+$",
                            re.I)
FAREWELL_MIN_NET = 20       # minutes of net before a goodbye can close it
FAREWELL_NEAR_END = 10      # ... and only from this many minutes before its scheduled end
FAREWELL_TAIL_MIN = 5       # keep recording this long after a goodbye (the last IDs and 73s)


def farewell(text: str) -> dict | None:
    """Net control's goodbye in this transmission -> {"kind": "close", "farewell": True, "phrase", "everyone"} or None.
    A question or a goodbye to one station ("thank you for checking in, Bob") still matches; accept() decides."""
    for s in _sentences(text):
        if s.rstrip().endswith("?") or _QUESTION_START.search(s):
            continue
        m = FAREWELL.search(s)
        if m and not _NEG_BEFORE.search(s[:m.start()]) and not _PLAN_BEFORE.search(s[:m.start()]) \
                and not _FUTURE_BEFORE.search(s[:m.start()]):
            return {"kind": "close", "farewell": True, "weak": False, "phrase": m.group(0),
                    "everyone": bool(EVERYONE.search(text))}     # "thank you ALL ... for your patience": any sentence
    return None


def accept_farewell(det: dict | None, who: str | None, ncs: str | None, minutes_in: float, near_end: bool) -> bool:
    """A goodbye closes the net only near its end, after a real net, addressed to everyone (a station checking out
    hears "thanks, have a good night" too), and from net control or a speaker not known to be someone else."""
    if not det or not det.get("farewell") or not near_end or minutes_in < FAREWELL_MIN_NET:
        return False
    if not det.get("everyone"):
        return False                  # to one station ("thanks for checking in Bob, have a good night"): not the end
    ncs = (ncs or "").upper() or None
    if who and ncs:
        return who == ncs
    return not who                    # a known speaker who is not net control never closes it


# "QRT" / "K7ABC clear" at the end: a close only from net control
WEAK_QRT = re.compile(r"\b(?:going\s+|now\s+|is\s+)?q\s?r\s?t\b", re.I)
WEAK_CLEAR = re.compile(r"\b(?P<c>" + _CALL + r")[,.]?\s+(?:is\s+)?(?:now\s+)?(?:clear|clearing|secured|securing|out|"
                        r"closing|qrt)(?:\s+and\s+listening)?\W*$", re.I)
# "the net will close in 5 minutes", "we're about to close the net"
SOON = re.compile(r"\b" + _NETW + r"\s+(?:will|is\s+going\s+to|'ll|is\s+about\s+to)\s+(?:be\s+)?(?:close|closing|"
                  r"closed|secure|end)\b|\b(?:about|ready|getting\s+ready|preparing)\s+to\s+(?:close|secure|end)\s+"
                  + _DET + _NAME + _NETW + r"\b", re.I)

_PLAN_BEFORE = re.compile(r"\b(?:before|after|until|till|til|when|whenever|once|if|unless|whether|prior\s+to|"
                          r"in\s+order\s+to|time\s+to|how\s+to|who|why)\b", re.I)
_NEG_BEFORE = re.compile(r"(?:\bnot|n't|\bnever|\bno\s+need\s+to|\bwon'?t|\bcannot|\bcan'?t)\s+(?:\w+\s+){0,3}$", re.I)
_SOON_BEFORE = re.compile(r"\b(?:be\s+closing|be\s+securing|about\s+to|ready\s+to|preparing\s+to|plan\s+to|"
                          r"planning\s+to|will\s+be\s+(?:closing|securing))\s+(?:\w+\s+){0,6}$", re.I)
_TIME_AFTER = re.compile(r"^\s*(?:at|in|around|about|by|after|on|tomorrow|next|soon|shortly|early|later|"
                         r"in\s+a\s+(?:few|couple|minute|moment|bit)|\d)", re.I)
_PAST_AFTER = re.compile(r"^\W*(?:last|yesterday|earlier|before)\b", re.I)
_CHECKINS_AFTER = re.compile(r"^\W*(?:to|for)\s+(?:\w+\s+){0,2}?(?:check|traffic|new|late|further|more|any)", re.I)
_NOW_AFTER = re.compile(r"^\W*(?:now|at\s+this\s+time|for\s+(?:the|this|to)\s?(?:night|evening|day|week|morning))\b", re.I)
_QUESTION_START = re.compile(r"^\W*(?:shall|should|can|could|may|do|does|did|is|are|will|would|when|what|how|why|"
                             r"who|any(?:one|body)?\s+(?:else\s+)?(?:before|want|need))\b", re.I)

# Net control's words while opening a net or taking roll (learn_ncs)
NCS_WORDS = re.compile(r"\bnet\s+control\b|\broll\s?call\b|\bpreamble\b|\bdirected\s+net\b|"
                       r"\b(?:calling|welcome\s+to|this\s+is)\s+the\s+(?:[\w'.&-]+\s+){0,6}?net\b|"
                       r"\b(?:taking|accepting|take|call(?:ing)?\s+for|open(?:ing)?\s+(?:the\s+net\s+)?for|"
                       r"begin(?:ning)?|start(?:ing)?|now\s+take|any\s+(?:other\s+|more\s+|further\s+)?)\s*"
                       r"(?:\w+\s+){0,2}?check-?ins\b|\b(?:open(?:ing)?|call(?:ing)?)\s+(?:the|this)\s+net\b|"
                       r"\bthe\s+net\s+is\s+(?:now\s+)?(?:open|called\s+to\s+order|in\s+session)\b", re.I)
_SELF_ID = re.compile(r"\b(?:this\s+is|this\s+be|here\s+is|de|my\s+call(?:sign)?\s+is)\s+(?P<c>" + _CALL + r")", re.I)


def _sentences(text: str) -> list[str]:
    """Sentences, each with its own end mark; quoted words are removed (a close inside a quote is not one)."""
    t = re.sub(r"[\"“”][^\"“”]{0,200}[\"“”]", " ", text or "")
    return [s.strip() for s in re.findall(r"[^.!?]+[.!?]*", t) if s.strip()]


def _judge(sentence: str, m) -> str | None:
    """'close', 'soon' or None for one match inside its sentence."""
    if sentence.rstrip().endswith("?") or _QUESTION_START.search(sentence):
        return None
    before = sentence[:m.start()]
    clause = re.split(r"[,;:]|\band\s+then\b", before)[-1]
    after = sentence[m.end():]
    if _NEG_BEFORE.search(before):
        return None
    if _PLAN_BEFORE.search(clause):
        return None
    if _CHECKINS_AFTER.search(after) or _PAST_AFTER.search(after):
        return None
    if _NOW_AFTER.search(after):
        return "close"
    if _SOON_BEFORE.search(before) or _TIME_AFTER.search(after):
        return "soon"
    return "close"


def detect(text: str, ncs: str | None = None) -> dict | None:
    """Did this transmission close the net? -> {"kind": "close" | "soon", "phrase", "weak": bool} or None.
    weak = "QRT" / "<call> clear": a close only when net control says it (`ncs`: its call, to match "<ncs> clear")."""
    best = None
    for s in _sentences(text):
        for p in STRONG:
            for m in p.finditer(s):
                k = _judge(s, m)
                if k == "close":
                    return {"kind": "close", "phrase": m.group(0), "weak": False}
                if k == "soon" and not best:
                    best = {"kind": "soon", "phrase": m.group(0), "weak": False}
        m = SOON.search(s)
        if m and not best and not s.rstrip().endswith("?") and not _PLAN_BEFORE.search(s[:m.start()]):
            best = {"kind": "soon", "phrase": m.group(0), "weak": False}
    last = (_sentences(text) or [""])[-1]
    m = WEAK_CLEAR.search(text or "")
    if m and ncs and m.group("c").upper() == ncs.upper():
        return {"kind": "close", "phrase": m.group(0).strip(), "weak": True, "call": ncs.upper()}
    m = WEAK_QRT.search(last)
    if m and not last.rstrip().endswith("?") and not _NEG_BEFORE.search(last[:m.start()]):
        return {"kind": "close", "phrase": m.group(0).strip(), "weak": True}
    return best


def accept(det: dict | None, who: str | None, ncs: str | None, minutes_in: float, in_window: bool) -> tuple[bool, str]:
    """Does a heard close count? -> (yes, how): "ncs" (net control said it), "wording" (net control's closing words,
    speaker unknown). `who`: the transmission's speaker (or None); `ncs`: net control (schedule or learned);
    `minutes_in`: minutes since the net's first transmission; `in_window`: inside the scheduled window (True when the
    conversation is not scheduled)."""
    if not det or det.get("kind") != "close":
        return False, ""
    ncs = (ncs or "").upper() or None
    if det.get("weak"):
        ok = bool(ncs) and (who == ncs or det.get("call") == ncs)
        return ok, "ncs" if ok else ""
    if who and ncs:
        return (who == ncs), "ncs" if who == ncs else ""
    if minutes_in < MIN_NET_MIN or not in_window:
        return False, ""
    return True, "wording"


def learn_ncs(views: list[dict]) -> str | None:
    """Net control, from the session itself: the first station that gives its own call ("this is N7XYZ") in a
    transmission with net control's words (roll call, check-ins, "calling the … net", "net control")."""
    if not views:
        return None
    t0 = datetime.fromisoformat(views[0]["start"])
    for v in views:
        if datetime.fromisoformat(v["start"]) - t0 > timedelta(minutes=LEARN_MIN):
            break
        text, calls = v.get("text") or "", list(v.get("callsigns") or [])
        if not NCS_WORDS.search(text):
            continue
        m = _SELF_ID.search(text)
        if m and (not calls or m.group("c").upper() in calls):
            return m.group("c").upper()
        if v.get("speaker") and v.get("speaker_how") in ("stated", "operator"):
            return v["speaker"]
    return None


class NetCloser:
    def __init__(self, station, cfg: dict | None = None):
        cfg = cfg or {}
        self.st = station
        self.enabled = bool(cfg.get("close_detect", True))
        self.tail = timedelta(minutes=float(cfg.get("close_tail_min", 2)))
        self.lock = threading.RLock()
        self.closes: dict[str, dict] = {}          # close key (net occurrence key, or "<radio>@<first tx>") -> close
        self.soon: dict[str, dict] = {}            # radio -> {"key", "at", "phrase"}: "closing soon" heard
        self.learned: dict[str, str] = {}          # session key -> net control learned from it
        self._load()

    # ---------- storage ----------
    @property
    def path(self):
        return self.st.data / "netclose.json"

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.closes = {k: v for k, v in (d.get("closes") or {}).items() if isinstance(v, dict)}
            self.learned = dict(d.get("learned") or {})
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        cut = (self.st.clock() - timedelta(hours=KEEP_H)).isoformat(timespec="seconds")
        self.closes = {k: v for k, v in self.closes.items() if (v.get("at") or "") >= cut}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"closes": self.closes, "learned": dict(list(self.learned.items())[-50:])},
                                      indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"netclose: not saved: {e}", flush=True)

    def state(self) -> dict:
        with self.lock:
            return {"closes": [dict(c) for c in self.closes.values()], "soon": dict(self.soon),
                    "learned": dict(self.learned), "tail_min": self.tail.total_seconds() / 60}

    def publish(self):
        self.st.bus.publish("netclose", self.state())

    # ---------- what the station asks ----------
    def holds(self, rid: str, key: str) -> bool:
        """This occurrence was closed and its radio paused for it: the schedule must not resume it (a restart, or
        "listen only during nets" switched on again, inside the window)."""
        c = self.closes.get(key)
        return bool(c and c["radio"] == rid and c["state"] == "closed" and c.get("paused"))

    def after_close(self, rid: str, at: datetime, net: dict | None) -> str | None:
        """A transmission starting at `at`: the key of the net closed before it (its tail over), so it starts a new
        conversation; None = carry on as usual. A different net starting on the radio is never affected."""
        with self.lock:
            for c in self.closes.values():
                if c["radio"] != rid or c["state"] != "closed" or (net and net.get("key") != c["key"]):
                    continue
                if c.get("tail_until") and at.isoformat(timespec="seconds") >= c["tail_until"] \
                        and at - datetime.fromisoformat(c["at"]) < timedelta(hours=6):
                    return c["key"]
        return None

    def _session(self, rid: str, at: str):
        with self.st.lock:
            tx = list(self.st.tx)
        ss = ics.sessions(tx, rid)
        return next((s for s in reversed(ss) if s["start"] <= at <= s["end"]), None), tx

    def _occurrence(self, rid: str, at: datetime):
        from .schedule import GRACE_MIN, occurrence_at
        ov = self.st.overrun(rid, at) if hasattr(self.st, "overrun") else None
        if ov:
            return ov, True                                   # running past its end: still its window
        o = occurrence_at(self.st.cfg.get("schedule") or [], rid, at)
        if not o:
            return None, False
        s, e = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
        return o, s <= at < e + timedelta(minutes=GRACE_MIN)

    def _sched_ncs(self, o) -> str | None:
        return ((o or {}).get("ncs") or "").upper() or None

    # ---------- hearing it ----------
    def on_tx(self, tx: dict):
        """A transmission's text is final (live, or the accurate pass): learn net control, hear a close."""
        if not self.enabled or tx.get("status") != "final" or not tx.get("start"):
            return
        rid, at_s = tx.get("radio"), tx["start"]
        at = datetime.fromisoformat(at_s)
        s, all_tx = self._session(rid, at_s)
        if not s:
            return
        tag = tx.get("net") or {}
        o, in_window = self._occurrence(rid, at) if tag.get("key") else (None, True)
        key = tag.get("key") or f"{rid}@{s['start']}"
        v = ics.view(tx)
        texts = [x for x in dict.fromkeys([v.get("text") or "", tx.get("text") or ""]) if x]
        changed = False
        with self.lock:
            ncs = self._sched_ncs(o) or self.learned.get(key)
            if not ncs and at - datetime.fromisoformat(s["start"]) <= timedelta(minutes=LEARN_MIN):
                got = learn_ncs([v | {"start": s["start"]}]) if v else None
                if got:
                    self.learned[key] = ncs = got
                    changed = True
                    print(f"{at:%H:%M:%S} {rid} net control learned: {got} ({key})", flush=True)
            c = self.closes.get(key)
            if c and (c["state"] in ("tail", "closed") or at_s <= c.get("undone_after", "")):
                if changed:
                    self._save()
                    self.publish()
                return
            who = v.get("speaker")
            minutes_in = (at - datetime.fromisoformat(s["start"])).total_seconds() / 60
            near_end = self._near_end(rid, at, o)
            if o and o.get("start"):                         # a scheduled net: from its start, not the last quiet gap
                minutes_in = max(minutes_in, (at - datetime.fromisoformat(o["start"])).total_seconds() / 60)
            for text in texts:
                det = detect(text, ncs)
                if not det:
                    fw = farewell(text) if tag.get("key") else None
                    if fw and accept_farewell(fw, who, ncs, minutes_in, near_end):
                        self._close(rid, key, tag, o, s, tx, by=who or ncs, how="goodbye", phrase=fw["phrase"],
                                    tail=max(self.tail, timedelta(minutes=FAREWELL_TAIL_MIN)))
                        return
                    continue
                if det["kind"] == "soon":
                    self.soon[rid] = {"key": key, "at": at_s, "phrase": det["phrase"]}
                    changed = True
                    continue
                ok, how = accept(det, who, ncs, minutes_in, in_window)
                if ok:
                    self._close(rid, key, tag, o, s, tx, by=who or ncs, how=how, phrase=det["phrase"])
                    return
            if changed:
                self._save()
                self.publish()

    def _near_end(self, rid: str, at: datetime, o: dict | None) -> bool:
        """Within FAREWELL_NEAR_END minutes of the occurrence's scheduled end, past it, or running over."""
        if not o or not o.get("end"):
            return False
        if o.get("overrun"):
            return True
        return at >= datetime.fromisoformat(o["end"]) - timedelta(minutes=FAREWELL_NEAR_END)

    def _close(self, rid, key, tag, o, s, tx, by, how, phrase, manual=False, tail=None):
        now = self.st.clock()
        inst = None
        try:
            inst = self.st.history.instance_of(rid, s["start"])
        except Exception:  # noqa: BLE001
            pass
        c = {"key": key, "radio": rid, "inst": inst or key, "name": (tag or {}).get("name") or (o or {}).get("name"),
             "net_id": (tag or {}).get("id"), "scheduled": bool(tag and tag.get("key")),
             "sched_start": (o or {}).get("start"), "sched_end": (o or {}).get("end"),
             "at": tx["start"] if tx else now.isoformat(timespec="seconds"), "tx": (tx or {}).get("id"),
             "by": by, "how": how, "phrase": phrase, "manual": manual, "session_start": s["start"] if s else None,
             "detected_at": now.isoformat(timespec="seconds"),
             "tail_until": (now + (timedelta(0) if manual else (tail or self.tail))).isoformat(timespec="seconds"),
             "state": "tail", "paused": False, "final": None}
        self.closes[key] = c
        self.soon.pop(rid, None)
        print(f"{now:%H:%M:%S} {rid} NET CLOSED{' by the operator' if manual else ''}: {c['name'] or key} at "
              f"{c['at'][11:16]} by {by or 'net control'} ({how}: {phrase!r})", flush=True)
        try:
            self.st.history.note_close(c["inst"], c["at"], "operator" if manual else (by or "net control"), c["tx"])
        except Exception as e:  # noqa: BLE001 — the history never stops the station
            print(f"history: {type(e).__name__}: {e}", flush=True)
        self._save()
        self.publish()
        if manual:
            self._finish(c)

    # ---------- after the tail ----------
    def tick(self, now: datetime | None = None):
        now = now or self.st.clock()
        due = []
        with self.lock:
            for c in self.closes.values():
                if c["state"] == "tail" and now.isoformat(timespec="seconds") >= c["tail_until"]:
                    due.append(c)
        for c in due:
            self._finish(c)

    def _ours(self, c) -> bool:
        """May the close pause the radio? Only one the station resumed for this net, or one listening only during
        nets, and only when the operator has not pressed Pause/Resume since the net's window opened."""
        from .schedule import LEAD_MIN
        if not c["scheduled"]:
            return False
        r = self.st.radios.get(c["radio"]) or {}
        L = r.get("listen") or {}
        if r.get("paused"):
            return False
        if L.get("resumed_for") == c["key"]:
            return True
        if L.get("scheduled"):
            op = L.get("operator_at")
            opened = (datetime.fromisoformat(c["sched_start"]) - timedelta(minutes=LEAD_MIN)).isoformat(
                timespec="seconds") if c.get("sched_start") else ""
            return not (op and op >= opened)
        return False

    def _finish(self, c):
        rid = c["radio"]
        r = self.st.radios.get(rid) or {}
        with self.lock:
            if c["state"] != "tail":
                return
            c["state"] = "closed"
        why = {"name": c["name"] or ("The net" if c["scheduled"] else "The conversation"), "at": c["at"],
               "by": c["by"], "manual": c["manual"]}
        if c["manual"]:
            if not r.get("paused"):
                c["resumed_for"] = (r.get("listen") or {}).get("resumed_for")
                self.st.set_paused(rid, True, by="operator")
                c["paused"] = True
                r["closed"] = why
        elif self._ours(c):
            self.st.set_paused(rid, True, by="schedule")
            c["paused"] = True
            r["closed"] = why
        s, _ = self._session(rid, c["at"])
        acc = getattr(self.st, "accurate", None)
        if s and acc is not None and acc.enabled:
            try:
                j = acc.submit(rid, s["start"], s["end"], auto=True, stage="final")
                c["final"] = {"key": j["key"], "start": s["start"], "end": s["end"]}
            except Exception as e:  # noqa: BLE001
                print(f"netclose: final pass: {type(e).__name__}: {e}", flush=True)
        try:
            self.st.history.flush([c["inst"]])                   # the minutes draft, with the whole net
        except Exception as e:  # noqa: BLE001
            print(f"history: {type(e).__name__}: {e}", flush=True)
        with self.lock:
            self._save()
        self.publish()

    # ---------- the operator ----------
    def end_now(self, rid: str, key: str | None = None) -> dict:
        """End net (the operator): the net or conversation on `rid` ends now; no tail."""
        if rid not in self.st.radios:
            raise KeyError(rid)
        with self.st.lock:
            fin = [t for t in self.st.tx if t.get("radio") == rid and t.get("status") == "final"]
        if not fin:
            raise ValueError("nothing heard on this radio yet")
        s, _ = self._session(rid, max(t["start"] for t in fin))
        if key:
            with self.st.lock:
                ss = ics.sessions(list(self.st.tx), rid)
            s = next((x for x in ss if (x["net"] or f"{rid}@{x['start']}") == key), None)
            if not s:
                raise LookupError(key)
        last = next(t for t in reversed(fin) if s["start"] <= t["start"] <= s["end"])
        tag = next((t.get("net") for t in fin if s["start"] <= t["start"] <= s["end"] and t.get("net")), None)
        key = (tag or {}).get("key") or f"{rid}@{s['start']}"
        with self.lock:
            c = self.closes.get(key)
            if c and c["state"] in ("tail", "closed"):
                if c["state"] == "tail":                         # heard already: end the tail now
                    c["manual"] = True
                    self._finish(c)
                return dict(self.closes[key])
            o, _ = self._occurrence(rid, datetime.fromisoformat(last["start"])) if tag else (None, True)
            self._close(rid, key, tag, o, s, last, by="operator", how="operator", phrase="End net", manual=True)
            return dict(self.closes[key])

    def undo(self, rid: str, key: str) -> dict:
        """Not closed / Keep listening: the net goes on (to its scheduled end); the radio is resumed if the close
        paused it; transmissions put in a new conversation after the close go back to the net."""
        with self.lock:
            c = self.closes.get(key)
            if not c or c["radio"] != rid:
                raise LookupError(key)
            was = c["state"]
            c.update(state="open", undone_after=self._last_start(rid) or c["at"])
            r = self.st.radios.get(rid) or {}
            paused_by_us = c.get("paused") and r.get("paused")
            c["paused"] = False
        if paused_by_us:
            self.st.set_paused(rid, False, by="schedule")
            if c.get("resumed_for"):
                r.setdefault("listen", {})["resumed_for"] = c["resumed_for"]
        r.pop("closed", None)
        self._retag(rid, key)
        try:
            self.st.history.undo_close(c["inst"])
        except Exception as e:  # noqa: BLE001
            print(f"history: {type(e).__name__}: {e}", flush=True)
        print(f"{self.st.clock():%H:%M:%S} {rid} net close UNDONE ({c['name'] or key}; was {was})", flush=True)
        with self.lock:
            self._save()
        self.publish()
        return dict(c)

    def _last_start(self, rid):
        with self.st.lock:
            s = [t["start"] for t in self.st.tx if t.get("radio") == rid]
        return max(s) if s else None

    def _retag(self, rid, key):
        """Transmissions after the close back into the net they followed (in memory, the log and the history)."""
        moved = []
        with self.st.lock:
            for t in self.st.tx:
                if t.get("radio") == rid and t.get("after") == key:
                    t.pop("after", None)
                    net = self.st.net_of(rid, datetime.fromisoformat(t["start"]), t.get("freq_mhz"))
                    if net and net.get("key") == key:
                        t["net"] = net
                    moved.append(dict(t))
        for t in moved:
            if t.get("status") == "final":
                self.st._log({"update": t["id"], "after": None, "net": t.get("net")})
            self.st.bus.publish("tx", t)

    def save_ncs(self, rid: str, key: str, call: str | None = None) -> dict:
        """"Save N7XYZ as net control for <net>": the learned (or given) call goes into the schedule."""
        call = (call or self.learned.get(key) or "").upper()
        if not call:
            raise ValueError("no net control heard for this net")
        nid = (self.closes.get(key) or {}).get("net_id") or (key.split("@")[0] if "@" in key else None)
        nets = self.st.cfg.get("schedule") or []
        n = next((x for x in nets if x.get("id") == nid), None)
        if not n:
            raise LookupError(key)
        body = {k: v for k, v in n.items() if k != "id"} | {"ncs": call}
        return self.st.nets.update(nid, body)
