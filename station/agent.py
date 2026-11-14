"""The station agent ("Sidekick" by default; the operator names it), DRAFT mode: it listens to a radio and writes, on
screen, what it would say and when. It never transmits (station/cat.py still refuses PTT; nothing here can key a radio).

The on-air name is the operator's (Settings → Station agent → On-air name). It is checked before it is saved: a name
that is a phonetic-alphabet word ("Echo"), a number, or a word that changes a call's meaning on the air ("Portable",
"Mobile", "Mayday", "Break") is refused, because "W9XYZ Echo" is heard as a different call (owner 2026-11-10: the
first name, Echo, sounded like a call with an extra E; renamed Sidekick, saved "Echo" settings migrate once).

Interactive, never queued (owner 2026-11-10): a draft is written as the frequency goes quiet. When the model cannot be
reached (no internet, a timeout) the OFFLINE RULES write the draft at once instead (check in when net control calls
for check-ins; when called by call or name, acknowledge and say the control operator will answer) and the model is
not tried again for a back-off (station/online.py). Nothing waits for the network to come back.

Per radio the agent remembers, across a restart (data/agent_state.json), when it last identified, whether it has
disclosed itself in the current net and whether it has checked in, so a restart mid-net does not check in twice.
TWO SPEEDS (owner 2026-11-10). The quick model decides every turn in well under a second: check-ins, acknowledgements,
short answers. A question that needs thinking gets a short "working on it" (kind working) and goes to a slower
reasoning model in the background while the net carries on. When the answer is ready, Sidekick waits for its place in
the net: when net control next asks for input ("any comments", "anything further for the net") it says "This is
<call> <name>, I have a response ready for the earlier inquiry from <station>" (kind ready); told to go ahead, it reads
the prepared answer word for word (kind answer; the code puts the prepared words in, the quick model never
paraphrases them). Outside a net, it offers the answer once the frequency is open after the asker. If the thinking
fails, it says so at the next opening and the control operator follows up. The offline rules follow the same steps.

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
from datetime import datetime, timedelta, timezone

from . import secrets
from .online import Online, is_network_error

DEFAULT_NAME = "Sidekick"
OLD_DEFAULT_NAMES = {"echo"}       # names an earlier version saved as its default: migrated once to DEFAULT_NAME
RULES = "rules"                    # the offline drafter's model id
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
PER_HOUR = 240            # model calls per hour, all radios (cost guard; past it the offline rules draft)
MODEL_TIMEOUT_S = 8       # a live draft that waits longer than this has missed its turn: the rules answer instead
DEEP_MODEL = "grok-4.5"   # the thinking model for a question that needs it (reasoning: seconds to a minute)
DEEP_TIMEOUT_S = 120      # past this the work has failed: said so at the next opening
ANSWER_WORDS = 70         # a prepared answer fits one transmission (MAX_WORDS with the ID)
QUEUE_MAX = 2             # questions waiting behind the one being answered (owner 11-12: anyone may ask it anything)
PAUSE_S = 3.5             # outside a net, a ready answer is given after this long with nobody transmitting
KEEP = 60                 # drafts kept in memory (all are appended to data/agent_drafts.jsonl)

PHONETIC = {"A": "Alfa", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf",
            "H": "Hotel", "I": "India", "J": "Juliett", "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November",
            "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango", "U": "Uniform",
            "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu", "0": "Zero", "1": "One",
            "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine"}

# Words a listener hears as part of a call or as a procedure word: never an on-air name.
_NOT_A_NAME = {w.lower() for w in PHONETIC.values()} | {
    "alpha", "juliet", "xray", "x-ray", "niner", "zero", "oh", "nil",
    "portable", "mobile", "maritime", "aeronautical", "stroke", "slash", "fixed", "qrp",
    "mayday", "pan", "pan-pan", "securite", "break", "over", "out", "clear", "roger", "copy", "wilco", "affirmative",
    "negative", "cq", "qrz", "qsl", "net", "control", "relay", "emergency", "priority", "traffic"}


def name_problem(name: str, call: str = "") -> str | None:
    """Why `name` cannot be the agent's on-air name, or None. It is spoken right after the call."""
    n = " ".join(str(name or "").split())
    if not re.fullmatch(r"[A-Za-z][A-Za-z '-]{1,23}", n):
        return "the name is 2 to 24 letters (spaces, ' and - allowed): it is spoken on the air"
    for w in re.split(r"[\s-]+", n.lower()):
        if w in _NOT_A_NAME:
            return (f"\"{w.title()}\" is a phonetic-alphabet or procedure word: after the call it would be heard as part "
                    f"of the call or as a signal (\"{(call or 'W9XYZ').upper()} {n}\")")
    if call and re.sub(r"[^a-z0-9]", "", n.lower()) == re.sub(r"[^a-z0-9]", "", call.lower()):
        return "the name cannot be the call"
    return None


def name_in(text: str, name: str) -> bool:
    """The name said in a transcript: any case, with or without a space or hyphen inside ("Side kick")."""
    letters = [c for c in (name or "") if c.isalnum()]
    if len(letters) < 2:
        return False
    return re.search(r"(?<![A-Za-z])" + r"[\s-]?".join(map(re.escape, letters)) + r"(?![A-Za-z])", text or "",
                     re.I) is not None


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

KINDS = ["check_in", "reply", "acknowledge", "stand_by", "say_again", "working", "ready", "answer", "sign_off", "none"]
STATION_KINDS = KINDS + ["present"]          # "present": a segment of an approved topic, said word for word (topics.py)
SCHEMA = {
    "type": "object",
    "properties": {
        "speak": {"type": "boolean"},
        "kind": {"type": "string", "enum": KINDS},
        "say": {"type": "string"},
        "wait_s": {"type": "number"},
        "why": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["speak", "kind", "say", "wait_s", "why", "question"],
    "additionalProperties": False,
}
THINK_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confident": {"type": "boolean"}, "why": {"type": "string"}},
    "required": ["answer", "confident", "why"],
    "additionalProperties": False,
}

SYSTEM = """You are {name}, the automated voice assistant of amateur radio station {call}. The licensee and control \
operator is {op}. You are in DRAFT mode: nothing you write is transmitted; the operator reads your drafts to judge you.

You listen to a frequency through speech recognition (it makes mistakes: calls may be misheard, words dropped). After \
each transmission decide whether {call} should transmit now, and if so exactly what to say, as an experienced, \
courteous ham operator would on voice.

Speak ONLY when:
- net control, in the latest transmission, explicitly calls for check-ins ("any check-ins", "stations wishing to check
  in, call now", "any further check-ins", "next station, please" while open check-ins are running) of a kind that
  fits this station, and {call} has not checked in yet this net: check in briefly. A roll call of places, groups or
  named stations ("now calling Springfield", "Riverside, log in as well", "next city", "moving on to the county EOC")
  is NOT this station's turn, even when it says "check in": many nets call their city stations, EOCs or members first
  and open to everyone later; wait for the general call. Some nets are only for certain stations
  (maritime mobile and its relays, a club's members, a served agency, stations with traffic, a roll call by name):
  if the net's purpose does not include an ordinary home station like this one, stay silent. Operators talking to
  each other, handing over net control or asking someone else to make calls are NOT a call for check-ins. When net
  control calls for a category of station ("maritime mobiles", "mobiles", "stations with traffic", "members",
  "emergency stations", a region), check in only if THIS station (described below) is in that category;
- someone calls {call} or {name} directly, or asks this station a question you can answer at once from what you know and the transcript: answer it, briefly (kind reply);
- someone asks this station a question that needs thinking, looking up or working out (anything you could not answer well in one breath): acknowledge it and say you are working on it and will report back (kind working), e.g. "{call} {name}, copy your question on <topic>, working on it, I'll report back when there's an opening." Put the question, restated clearly in one line with who asked, in "question". Never guess at the answer now;
- net control acknowledges or recognizes {call} and expects an answer;
- the copy was too poor to answer: ask them to say again (kind say_again).
Prepared answers (see "Your prepared answer" below, when there is one): IN A NET, when it is READY wait for your place: only when net control asks for input, comments, further business, questions or "anyone else". Then say only that you have it (kind ready): "This is {call} {name}, I have a response ready for the earlier inquiry from <station>." Never read it before you are told to go ahead. When net control or the asker tells {call}/{name} to go ahead, or the asker or {op} asks {call}/{name} for it, give kind answer (the station reads the prepared words itself; leave "say" empty). OUTSIDE A NET (a conversation), give it at a pause (kind answer): when the frequency has been quiet a few seconds, when a station turns it over, or when asked for it; the station says "This is {call} {name}, for <station>:" before the words. If the work FAILED, at the same kind of opening say briefly that you could not get the answer and {op} will follow up (kind reply). More than one question may be waiting: they are answered in the order asked.
Otherwise stay silent (speak false, kind none). Never break into another conversation, never call CQ, never talk \
just to fill silence. During emergency or priority traffic stay silent unless net control asks this station directly.

First transmission in a contact, shaped like: "{call}, {name}, an automated assistant, {op} is control operator, \
<the rest>" (say the call once at the start; repeat it at the end only when identification is due). A check-in: \
"{call}, {name}, an automated assistant for {op}, {op} is control operator. Checking in, no traffic." The \
operator's check-in line gives facts about the station (a name, a place) to use in your own words; never say it as \
if you were {op}, and never repeat the call inside one transmission except as the closing ID.

Voice protocol: use the other station's call when answering; "over" only on HF when turn-taking is unclear; standard \
phonetics for calls and spelled words; plain language, no Q-codes except common ones (QSL, QRZ, QTH) used naturally; \
no jargon the other side might not know. Sound human-natural and brief (one to three sentences, under 70 words).

FCC Part 97 rules you must follow (the station's software also enforces them and will block a draft that breaks one):
- Identify with {call} at the end of each exchange and at least every ten minutes; never use an SSID like -20 on voice.
- Never pretend to be a person. On your first transmission in a contact or net, say you are {call}'s automated \
assistant and that {op} is control operator (both, in those words).
- No music, no obscene or indecent language, no codes or ciphers that hide meaning.
- No business: never buy, sell, advertise or arrange anything for pay; no messages for anyone's employer.
- No broadcasting to the public; no relaying messages on someone else's behalf (third-party traffic) - tell them \
{op} will handle it personally.
- Never give legal, medical or emergency-response instructions as fact; for an emergency, say {op} is being alerted.
- If you are unsure whether something is allowed, stay silent and explain why in "why".

Answer with JSON only: {{"speak": bool, "kind": one of {kinds}, "say": "the exact words, or empty", \
"wait_s": seconds to wait after the frequency goes quiet (1.5-2.5 normally, 0.8 for a quick acknowledgement), \
"why": "one short line for the operator", "question": "for kind working: the question, restated in one line with who asked; otherwise empty"}}."""

DEEP_SYSTEM = """You are {name}, the automated assistant of amateur radio station {call} (control operator {op}). On the air, {asker} asked: "{question}". You said you would report back. Work out the answer now; it will be read aloud on an amateur radio frequency, when net control gives you the floor.

Write the words to be spoken: at most {words} words, plain spoken English, numbers as a person says them, standard phonetics for any call sign, no lists, no URLs read character by character, no Q-codes beyond QSL/QTH. Start with the station that asked ("{asker}, ..."); do not add the station's ID (the station adds it). If you cannot answer reliably, say so briefly and that {op} will follow up; never invent facts. FCC Part 97: no business or prices, no relaying messages for others, no music, no codes; never give legal, medical or emergency-response instructions as fact (say {op} will follow up). Answer with JSON: {{"answer": "the spoken words", "confident": bool, "why": "one line for the operator: how you know"}}."""


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
    auto = re.search(r"\bautomat\w*\b", say, re.I) is not None
    named = re.search(r"\bcontrol operator\b", say, re.I) is not None
    disclosed = auto and named
    rule("97.113(a)(4) disclosure", disclosed or not ctx["first"],
         "says it is automated and names the control operator" if disclosed else
         ("first transmission must also name the control operator" if auto else
          "first transmission must say it is an automated assistant and name the control operator")
         if ctx["first"] else "disclosed earlier")
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
    cap = int(ctx.get("present_words") or MAX_WORDS) if d.get("kind") == "present" else MAX_WORDS
    rule("transmit cap", words <= cap, f"{words} words (about {words * 0.4:.0f} s)")
    ok, why = privileges(ctx.get("freq_mhz"), ctx.get("mode"), ctx.get("license_class"))
    rule("97.301/97.305 privileges", ok is not False, why)
    rule("97.105/97.109(d) control operator", True, "Draft mode: nothing is transmitted")
    return out


# ---------------------------------------------------------------- the offline rules --------------------------------
CHECKIN_CALL = re.compile(
    r"\b(any (more |further |other |additional |late |final |new )?(check[- ]?ins?|stations? (for (the )?(net|check)|"
    r"to check|wishing|wanting)|takers)|(stations?|anyone|anybody|centers?)\b[^.?!]{0,60}\b(wishing|wanting|who would like|"
    r"that would like|would like|desiring) to check[- ]?in|(taking|take|open(ing)? (the net )?for|calling for|"
    r"now for|accepting) (any )?check[- ]?ins?|check[- ]?ins?,? (please|go ahead|call now|are open|now)|"
    r"please call now|call now with your call|any (visitors|visiting stations|late or missed|missed or late)|"
    r"(visitors|visiting stations|any other stations?|stations not (yet )?(on the|in the) (roster|list))\b[^.?!]{0,40}"
    r"\b(come now|call now|check[- ]?in|please))", re.I)
# during open check-ins, net control hands the frequency on with "next station, please"
NEXT_STATION = re.compile(r"\b(next|first|another) station,? (please|go ahead)\b|\bnext (check[- ]?in|one),? please\b",
                          re.I)
# net control working through a list of places or named stations (not this station's turn)
ROLL_CALL = re.compile(r"\b(now calling|calling (for )?the|log(ged)? in as well|(next|now) (city|county|town|eoc|"
                       r"station on the list)|moving (on )?to|let'?s go (now )?to|now going to|we'?ll go to)\b", re.I)
OPEN_TO_ALL = re.compile(r"\b(any station,? any ?where|any and all|all stations|any station|with or without traffic)\b"
                         r"(?!,?\s+(holding|with traffic|having|that (has|have|is|are)|who (has|have|is|are)|in|from|located))",
                         re.I)
# net control saying check-ins come LATER (an HF preamble is often lost: "Before I start taking check-ins, ...")
NOT_YET = re.compile(r"\b(before (i|we) (start|begin|take|open|go to)|(i'?ll|i will|we'?ll|we will|i'?m going to|we'?re going to)"
                     r" (be )?(start|begin|take|open|call|go)\w*|in (a|just a) (moment|minute|few minutes)|shortly|"
                     r"after (the|this) (preamble|announcements?|bulletins?|traffic))\b", re.I)
# "please call now" alone: who is called was said earlier (maybe not heard). A check-in word must be near it.
BARE_CALL = re.compile(r"^\W*(please,? )?(call|come) (now|in)\W*$|^\W*(please,? )?call now with your call\W*$", re.I)
CHECKIN_WORD = re.compile(r"\bcheck[- ]?ins?\b|\bvisitors?\b|\bstations?\b|\btakers\b", re.I)
# a roll call by place: the state names come from the US Census list in the callbook folder (hamkit/places.py; none
# are written here). Without it, the shape of the call: "Stations Colorado. Check in now."
_PLACE_RE: dict[tuple, re.Pattern] = {}
_NOT_PLACE = {"please", "any", "all", "with", "without", "holding", "wishing", "check", "who", "that", "only", "listening",
              "go", "call", "come", "now", "net", "control", "on", "the", "a", "and", "or", "mobile", "portable", "for"}
PLACE_SHAPE = re.compile(r"\b[Ss]tation'?s?,?\s+(?:in |from |of )?(?:the )?(?:state of )?([A-Z][a-z]+(?: [A-Z][a-z]+)?)(?=\s*[.,])")


def _place_names() -> list[str]:
    try:
        from hamkit import places
        return places.names()
    except Exception:  # noqa: BLE001
        return []


def named_places(text: str) -> set[str]:
    """Places named in `text` (lower case)."""
    names = _place_names()
    if names:
        key = tuple(names)
        rx = _PLACE_RE.get(key)
        if rx is None:
            rx = _PLACE_RE[key] = re.compile(r"\b(" + "|".join(sorted((re.escape(n) for n in names), key=len,
                                                                     reverse=True)) + r")\b", re.I)
        return {m.group(0).lower() for m in rx.finditer(text)}
    return {m.group(1).lower() for m in PLACE_SHAPE.finditer(text) if m.group(1).split()[0].lower() not in _NOT_PLACE}

# kinds of station a fixed home station is not (said in the sentence that calls for check-ins)
RESTRICTED = re.compile(
    r"\b(maritime|mobiles?( only)?|members? only|(only )?stations? with (formal |listed )?traffic|traffic only|"
    r"holding (any )?(formal |listed |emergency |priority )?traffic|stations? with traffic|"
    r"liaison|emergency (stations?|traffic) only|priority (stations?|traffic) only|short[- ]?time|"
    r"(emergency|medical|priority|formal|health|welfare|listed)[^.?!]{0,30}\btraffic|relays?\b|"
    r"(city|county|state|agency|hospital|shelter|school) stations?|(emergency )?operations? cent(er|re)s?|eocs?|"
    r"from the (state|city|county)|in (the )?(county|state|section) of)\b", re.I)


def _sentence_at(text: str, i: int) -> str:
    a = max(text.rfind(".", 0, i), text.rfind("?", 0, i), text.rfind("!", 0, i)) + 1
    ends = [j for j in (text.find(".", i), text.find("?", i), text.find("!", i)) if j >= 0]
    return text[a:(min(ends) + 1) if ends else len(text)]


def _sentences_before(text: str, i: int, n: int = 2) -> str:
    """The `n` sentences before position i (net control says who is called, then "check in now")."""
    parts = [x for x in re.split(r"(?<=[.?!…])\s+", text[:i]) if re.search(r"\w", x)]
    return " ".join(parts[-n:])


def _gap_s(a: dict, b: dict) -> float | None:
    """Seconds from the end of transmission a to the start of b."""
    try:
        end = datetime.fromisoformat(str(a["start"])) + timedelta(seconds=float(a.get("duration_s") or 0))
        return (datetime.fromisoformat(str(b["start"])) - end).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def _scope(rows: list[dict], idx: int, at: int) -> str:
    """Who net control called: the calling sentence and the sentence before it; when the call opens its transmission,
    the transmission before too if it ended under 10 s earlier (a pause cut the sentence in two). Wider than that,
    an earlier "he relays Arizona" was read as a restriction on the next call (12th Region roll call, 11-12)."""
    text = rows[idx].get("text") or ""
    before = _sentences_before(text, at, 1)
    out = before + " " + _sentence_at(text, at)
    if idx > 0 and not before.strip():
        g = _gap_s(rows[idx - 1], rows[idx])
        if g is not None and g < 10:
            out = _sentences_before(rows[idx - 1].get("text") or "", len(rows[idx - 1].get("text") or ""), 1) + " " + out
    return out


def _restriction(scope: str, sent: str, region: str, net_name: str = "") -> str:
    """Why this station is not the one called ("" = it is)."""
    if NOT_YET.search(sent):
        return "net control will take check-ins later"
    if OPEN_TO_ALL.search(sent):
        return ""
    near = re.sub(re.escape(net_name), " ", scope, flags=re.I) if net_name else scope   # a net named after its state is not a roll call
    named = named_places(near)
    if named:                                          # a roll call by place: "Stations Colorado. Check in now."
        if {p for p in named if p in (region or "").lower()}:
            return ""
        where = ", ".join(sorted(x.title() for x in named))
        return (f"check-ins called for stations in {where}" +
                ("" if region else " (set this station's region in Settings → Station agent to answer a call for it)"))
    if RESTRICTED.search(scope):
        return "check-ins called for a kind of station this one is not"
    if BARE_CALL.match(sent.strip()) and not CHECKIN_WORD.search(scope.replace(sent, "")):
        return "a call with no word of who is called (the start of it was not heard)"
    return ""


def checkin_invited(rows: list[dict], region: str = "", net_name: str = "") -> tuple[bool, str]:
    """(invited, why) from the latest transmission, with the open check-ins before it (last 10 transmissions).
    Who is called is read around the call, not only in its sentence (2026-11-12 review: "Any station holding traffic,
    traffic only. Check in now." drafted a check-in from a station with none)."""
    text = (rows[-1].get("text") or "") if rows else ""
    m = CHECKIN_CALL.search(text)
    if m:
        why = _restriction(_scope(rows, len(rows) - 1, m.start()), _sentence_at(text, m.start()), region, net_name)
        return (False, why) if why else (True, "net control called for check-ins")
    if NEXT_STATION.search(text):
        for k in range(len(rows) - 2, max(-1, len(rows) - 11), -1):
            t = rows[k]
            mm = CHECKIN_CALL.search(t.get("text") or "")
            if mm:
                why = _restriction(_scope(rows, k, mm.start()), _sentence_at(t["text"], mm.start()), region, net_name)
                if why:
                    return False, "open check-ins: " + why
                return True, "net control asked for the next station during open check-ins"
    return False, ""


# net control acknowledging this station ("W9XYZ, thank you, you're in the log"): a check-in taken, not a call to answer
ACK = re.compile(r"\b(thank(s| you)|got (you|ya|it)|(you'?re|you are) (in the log|logged|on the list|in)|logged|welcome|"
                 r"good (morning|afternoon|evening|to hear you)|copy|roger|noted|appreciate)\b", re.I)
ASKS = re.compile(r"\?|\b(go ahead|do you (have|copy)|are you|can you|could you|would you|what|how|where|when|"
                  r"anything (for|further)|traffic for|comments?)\b", re.I)


def acknowledged(text: str) -> bool:
    """Net control named this station to acknowledge it, not to ask it anything."""
    return bool(ACK.search(text or "")) and not ASKS.search(text or "")


# emergency or priority traffic on ANY net (97.101(c)), not only on nets flagged as emergency nets
BREAK = re.compile(r"\b(break,? break|emergency traffic|priority traffic|mayday|may day|this is an emergency)\b", re.I)

# net control giving the floor: "any comments", "anything further for the net", "anyone else", "questions?"
INPUT_CALL = re.compile(
    r"\b(any (other |more |further |final |last )?(comments?|questions?|business|announcements?|input|traffic|"
    r"thing (else |further )?for the net)|anything (else |further |more )?(for the net|before (i|we) close)|"
    r"anyone else|anybody else|(further|other|new) business|open (the )?floor|comments or questions|"
    r"(the )?floor is open)", re.I)
GO_AHEAD = re.compile(r"\b(go ahead|go on|let'?s hear it|proceed|you have the (floor|frequency)|"
                      r"what (have you got|do you have|is it|did you (find|learn|come up with))|"
                      r"(give|share|read) (us |me )?(your |the |that )?(answer|response|findings)|"
                      r"(your|the) (answer|response) (please|now)|are you ready|do you have (an|the|that) answer)\b", re.I)
# a station handing the frequency on in a conversation: the end of its transmission
TURNOVER = re.compile(r"\b(over|back to you|back over to you|go ahead|your turn|what do you think|turn it over)\W*$", re.I)


def _direct_answer_place(ctx: dict, p: dict, text: str) -> bool:
    """Outside a net, a READY answer is given at a pause (owner 11-12): the frequency quiet PAUSE_S, a turn-over,
    or the station called."""
    return (p.get("state") == "ready" and not ctx.get("in_net")
            and (ctx.get("trigger") in ("answer ready", "pause") or bool(TURNOVER.search(text or ""))
                 or bool(ctx.get("addressed"))))


def fixed_lines(call: str, name: str, op: str) -> dict:
    """The words the rules say, built in one place: rules_draft says them, and the voice keeps a clip of each ahead
    of time (station/tts.py prerender) so a check-in or an acknowledgement is not waiting on the voice service."""
    intro = f"{call}, {name}, an automated assistant, {op} is control operator"
    return {"check_in_first": f"{intro}, checking in, no traffic. {call}.",
            "check_in": f"{call}, checking in, no traffic.",
            "ack_first": f"{intro}. I copy you; {op} will answer you personally. {call}.",
            "ack": f"Copy, {op} will come back to you personally. {call}."}


def rules_draft(rows: list[dict], ctx: dict, checked_in: bool, pending: dict | None = None) -> dict:
    """The draft without a model: the moments a station must not miss, written from fixed words."""
    call, name, op = ctx["call"], ctx["name"], ctx["op"]
    last = rows[-1] if rows else {}
    text = last.get("text") or ""
    silent = {"speak": False, "kind": "none", "say": "", "wait_s": 2.0, "question": ""}
    intro = f"{call}, {name}, an automated assistant, {op} is control operator"
    if not rows:
        return silent | {"why": "nothing heard"}
    if ctx.get("emergency"):
        return silent | {"why": "emergency or priority traffic: silent"}
    p = pending or {}
    if _direct_answer_place(ctx, p, text):
        return {"speak": True, "kind": "answer", "say": "", "wait_s": 1.5, "question": "",
                "why": "outside a net: the prepared answer at a pause"}
    if p.get("state") in ("ready", "announced") and ctx.get("addressed") and GO_AHEAD.search(text):
        return {"speak": True, "kind": "answer", "say": "", "wait_s": 1.5, "question": "",
                "why": "told to go ahead after announcing the answer"}
    if p.get("state") in ("ready", "failed") and INPUT_CALL.search(text):
        if p["state"] == "ready":
            say = (f"This is {call} {name}, I have a response ready for the earlier inquiry from "
                   f"{p.get('asker') or 'a station'}." + (f" {intro}." if ctx["first"] else ""))
            return {"speak": True, "kind": "ready", "say": say, "wait_s": 2.0, "question": "",
                    "why": "net control asked for input: announce the prepared answer"}
        say = (f"{call} {name}: I could not get an answer to the earlier inquiry from {p.get('asker') or 'a station'}; "
               f"{op} will follow up. {call}.")
        return {"speak": True, "kind": "reply", "say": say, "wait_s": 2.0, "question": "",
                "why": "the thinking failed: say so at the opening"}
    if ctx.get("addressed"):
        fl = fixed_lines(call, name, op)
        say = fl["ack_first"] if ctx["first"] else fl["ack"]
        return {"speak": True, "kind": "acknowledge", "say": say, "wait_s": 1.5, "question": "",
                "why": "called by call or name: acknowledge, the control operator answers"}
    invited, why = checkin_invited(rows)
    if invited or why:
        if checked_in:
            return silent | {"why": "check-ins called, already checked in"}
        if not invited:
            return silent | {"why": why + ": silent"}
        fl = fixed_lines(call, name, op)
        say = fl["check_in_first"] if ctx["first"] else fl["check_in"]
        return {"speak": True, "kind": "check_in", "say": say, "wait_s": 2.0, "question": "", "why": why}
    return silent | {"why": "not called and no call for check-ins"}


def facts() -> list[str]:
    """What the station knows for certain, given to the thinking model as authoritative (11-11 rehearsal: asked for
    General phone privileges it said 40 m phone starts at 7.200; it is 7.175)."""
    out = ["Authoritative facts from the station (use these over your memory):",
           "US amateur phone (voice) privileges, MHz (47 CFR 97.301/97.305): " + "; ".join(
               f"{c.title()}: " + ", ".join(f"{a:g}-{b:g}" for a, b in PHONE[c]) for c in ("technician", "general",
                                                                                         "advanced", "extra")),
           "60 m: five channels, USB only, General and above, 100 W ERP (dial 5.3305, 5.3465, 5.3570, 5.3715, "
           "5.4035 MHz). 30 m (10.1 MHz) has no phone for anyone."]
    return out


def allowed_kinds(ctx: dict, checked_in: bool, pending: dict | None, text: str) -> tuple[list[str], str]:
    """The moves open to the station after this transmission, read by the station itself (owner 11-10: Sidekick fits
    the net's protocol). Only these are offered to the model; none = the station stays silent without asking it.
    11-10 replays of four real nets: offered every move, the quick model tried to check in on 29 turns of one roll
    call although told check-ins were not open."""
    if ctx.get("asked"):
        return list(KINDS), "asked by the operator"
    p = pending or {}
    if ctx.get("emergency") and not ctx.get("addressed"):
        return ["none"], "emergency or priority traffic"
    out = []
    if ctx.get("invited") and not checked_in:
        out.append("check_in")
    if ctx.get("addressed"):
        out += ["reply", "acknowledge", "say_again", "stand_by"]
        if p.get("state") not in ("working", "ready", "announced") or not ctx.get("queue_full"):
            out.append("working")
    # 11-11 rehearsal: offered whenever ready, the quick model announced it right after net control moved on to a
    # topic. Its place: net control asking for input, someone calling this station, or (outside a net) an open
    # frequency after the answer became ready.
    if p.get("state") == "ready" and ctx.get("in_net") and (INPUT_CALL.search(text or "") or ctx.get("addressed")):
        out.append("ready")
    if _direct_answer_place(ctx, p, text):
        out.append("answer")
    if p.get("state") in ("ready", "announced") and (ctx.get("addressed") or GO_AHEAD.search(text or "")):
        out.append("answer")
    if p.get("state") == "failed" and (ctx.get("addressed") or INPUT_CALL.search(text or "")):
        out.append("reply")
    if not out:
        return ["none"], ("already checked in; " if checked_in else "") + \
            "not called, no check-ins open to this station, no answer waiting"
    return list(dict.fromkeys(out + ["none"])), ""


# ---------------------------------------------------------------- providers --------------------------------------
def call_xai(key, model, system, user, timeout=MODEL_TIMEOUT_S, schema=SCHEMA, max_tokens=300, name="draft"):
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system},
                                                                  {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema,
                                                                       "strict": True}}}
    req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return json.loads(d["choices"][0]["message"]["content"]), d.get("usage") or {}


def _xai_post(key, body, timeout):
    req = urllib.request.Request("https://api.x.ai/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call_xai_tools(key, model, system, user, toolbox, timeout=MODEL_TIMEOUT_S, schema=SCHEMA, max_tokens=300,
                   name="draft", rounds=4, post=None):
    """call_xai, but the model may first call the toolbox's tools (station/toolbox.py) for up to `rounds` rounds; the
    answer is then asked for in the schema with no tools (so its shape never depends on tool calling). Same return
    as call_xai; usage also lists the tools called."""
    post = post or _xai_post
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    end = time.monotonic() + timeout
    usage = {"tools": []}

    def add(d):
        for k, v in (d.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] = usage.get(k, 0) + v
    for _ in range(rounds):
        d = post(key, {"model": model, "max_tokens": max_tokens, "messages": msgs, "tools": toolbox.specs,
                       "tool_choice": "auto"}, max(5.0, end - time.monotonic()))
        add(d)
        m = d["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        if not calls:
            break
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function") or {}
            usage["tools"].append(fn.get("name"))
            msgs.append({"role": "tool", "tool_call_id": c.get("id"),
                         "content": toolbox.run(fn.get("name"), fn.get("arguments"))})
    d = post(key, {"model": model, "max_tokens": max_tokens, "messages": msgs,
                   "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema,
                                                                              "strict": True}}},
             max(5.0, end - time.monotonic()))
    add(d)
    return json.loads(d["choices"][0]["message"]["content"]), usage


def call_anthropic(key, model, system, user, timeout=MODEL_TIMEOUT_S, schema=SCHEMA, max_tokens=1000, name="draft"):
    import anthropic                                   # install/windows/requirements-windows.txt
    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0)
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}],
                                  output_config={"effort": "low" if schema is SCHEMA else "medium",
                                                 "format": {"type": "json_schema", "schema": schema}})
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}


TOOLS_NOTE = ("\n\nTools: you can call search_library (this station's library: the operator's documents and manual), "
              "list_sites and path_analysis (computed terrain paths between the operator's named sites). Call them "
              "BEFORE answering anything about this station, its procedures or whether two places can talk; use their "
              "results as authoritative, never estimate a path, and say plainly when a tool found nothing.")

PROVIDERS = {"xai": ("XAI_API_KEY", call_xai), "anthropic": ("ANTHROPIC_API_KEY", call_anthropic)}


# ---------------------------------------------------------------- the agent --------------------------------------
class Agent:
    """Drafts per radio. Settings in data/agent.json (never committed): name, check-in line, operator's first name,
    license class, model, compare models, and each radio's mode ("off" | "draft"). Per-radio memory in
    data/agent_state.json."""

    def __init__(self, station, providers=None, log_path=None, state_path=None, per_hour=PER_HOUR):
        self.st = station
        self.providers = providers or PROVIDERS
        self.path = station.data / "agent.json"
        self.log_path = log_path or station.data / "agent_drafts.jsonl"
        self.state_path = state_path or station.data / "agent_state.json"
        self.per_hour = per_hour
        self.lock = threading.Lock()
        self.speculate = True                                  # start the model at on_final (see draft)
        self._spec: dict[str, dict] = {}
        self.cfg = {"enabled": False,                    # owner 11-10: off by default; the operator turns it on
                    "name": DEFAULT_NAME, "checkin": "", "operator": "", "station": "", "region": "", "license_class": "",
                    "model": DEFAULT_MODEL, "deep_model": DEEP_MODEL, "compare": [], "radios": {},
                    "voice": "", "speed": 1.0}                   # the voice the operator chose (station/tts.py)
        saved = None
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.cfg.update(saved)
        except (OSError, ValueError):
            pass
        if saved is not None and str(self.cfg.get("name") or "").strip().lower() in OLD_DEFAULT_NAMES | {""}:
            self.cfg["name"] = DEFAULT_NAME                     # the old default, never chosen: becomes Sidekick
            try:
                self._save()
            except OSError:
                pass
        self.drafts: deque = deque(maxlen=KEEP)
        self.calls: deque = deque()                 # monotonic times of model calls (rate guard)
        self.timers: dict[str, threading.Timer] = {}
        self.online = {p: Online(p) for p in self.providers}
        self._probe_stop = threading.Event()
        self.state: dict[str, dict] = self._load_state()   # radio -> {"last_id", "disclosed", "checked_in", "pending"}
        from .sender import Sender
        self.sender = Sender(station, self)                 # Send mode (station/sender.py): off unless chosen
        from .topics import Topics
        self.topics = Topics(station.data)                  # prepared talks and the operator's sites (data/, private)
        from .chat import Chat
        self.chat = Chat(self)                              # the operator's side chat (station/chat.py): never sent
        self._lib = None
        self._lib_lock = threading.Lock()
        self._to_send: dict[str, tuple] = {}

    # ----- is the model reachable? (Sidekick's card says so before a turn needs it) -----
    PROBE_EVERY_S = 120

    def probe(self) -> dict | None:
        """One free request (the model list) when Sidekick is on, a key is set and a radio is in Draft; updates the
        online state the card shows. Returns the new mode_now when it changed."""
        if not self.cfg.get("enabled") or not any(self.mode(r) in ("draft", "send") for r in self.st.radios):
            return None
        prov = MODELS.get(self.cfg.get("model") or DEFAULT_MODEL, {}).get("provider", "xai")
        keyname, _ = self.providers.get(prov, (None, None))
        key = secrets.read(self.st.root, keyname) if keyname else None
        if not key or prov != "xai":
            return None
        on = self.online.setdefault(prov, Online(prov))
        if not on.should_try():
            return None
        before = on.online
        try:
            req = urllib.request.Request("https://api.x.ai/v1/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=8):
                pass
            on.ok()
        except Exception as e:  # noqa: BLE001
            if is_network_error(e):
                on.failed(e)
            else:
                on.ok()                                    # an answer (401...) is not offline
        if on.online != before:
            keys = {p: secrets.status(self.st.root, k)["set"] for p, (k, _) in self.providers.items()}
            m = self._mode_now(keys)
            self.st.bus.publish("agent", {"mode_now": m})
            return m
        return None

    def start_probe(self):
        def loop():
            while not self._probe_stop.wait(5 if not any(o.online is not None for o in self.online.values())
                                             else self.PROBE_EVERY_S):
                try:
                    self.probe()
                except Exception as e:  # noqa: BLE001
                    print(f"agent probe: {type(e).__name__}: {e}", flush=True)
        threading.Thread(target=loop, daemon=True, name="agent-probe").start()

    # ----- memory across a restart -----
    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for rid, s in (raw or {}).items():
            if not isinstance(s, dict):
                continue
            d = {k: s[k] for k in ("disclosed", "checked_in", "checked_in_at", "checked_in_how", "muted") if s.get(k)}
            if isinstance(s.get("pending"), dict) and s["pending"].get("state") in ("ready", "announced", "failed"):
                d["pending"] = s["pending"]                     # work in flight is lost with the process: not kept
            qq = [x for x in s.get("queue") or [] if isinstance(x, dict) and x.get("state") in ("ready", "failed")]
            if qq:
                d["queue"] = qq
            try:
                if s.get("last_id"):
                    d["last_id"] = datetime.fromisoformat(s["last_id"])
            except ValueError:
                pass
            out[rid] = d
        return out

    def _save_state(self):
        doc = {rid: {k: (v.isoformat(timespec="seconds") if isinstance(v, datetime) else v) for k, v in s.items()
                     if k in ("last_id", "disclosed", "checked_in", "checked_in_at", "checked_in_how", "muted", "pending",
                              "queue")}
               for rid, s in self.state.items()}
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # ----- settings -----
    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def name(self) -> str:
        return self.cfg.get("name") or DEFAULT_NAME

    def settings(self, body: dict) -> dict:
        with self.lock:
            for k in ("name", "checkin", "operator", "station", "region"):
                if k in body:
                    v = re.sub(r"\s+", " ", str(body[k] or "")).strip()[:120 if k in ("checkin", "station", "region") else 24]
                    if k == "name":
                        v = v or DEFAULT_NAME
                        why = name_problem(v, self.st.me or "")
                        if why:
                            raise ValueError(f"on-air name: {why}")
                    self.cfg[k] = v
            if "enabled" in body:
                self.cfg["enabled"] = bool(body["enabled"])
            if "license_class" in body:
                v = str(body["license_class"] or "").lower()
                if v and v not in PHONE:
                    raise ValueError("license class: technician, general, advanced or extra")
                self.cfg["license_class"] = v
            if "model" in body:
                if body["model"] not in MODELS:
                    raise ValueError("unknown model")
                self.cfg["model"] = body["model"]
            if "deep_model" in body:
                if body["deep_model"] not in MODELS:
                    raise ValueError("unknown thinking model")
                self.cfg["deep_model"] = body["deep_model"]
            if "compare" in body:
                c = [m for m in (body["compare"] or []) if m in MODELS and m != self.cfg["model"]][:2]
                self.cfg["compare"] = c
            if "voice" in body:
                v = str(body["voice"] or "").strip().lower()
                if v and not re.fullmatch(r"[a-z0-9_-]{2,40}", v):
                    raise ValueError("voice: a voice name from the list")
                self.cfg["voice"] = v
            if "speed" in body:
                f = float(body["speed"] or 1.0)
                if not 0.7 <= f <= 1.5:
                    raise ValueError("speed: 0.7 to 1.5")
                self.cfg["speed"] = round(f, 2)
            self._save()
        if self.cfg.get("enabled") and any(k in body for k in ("enabled", "name", "operator", "voice", "speed")):
            self.st.tts.prerender()                             # the fixed lines' clips, ahead of the first send
        return self.view()

    def set_radio(self, rid: str, mode: str) -> dict:
        if rid not in self.st.radios:
            raise ValueError(f"no radio called {rid!r}")
        if mode not in ("off", "draft", "send"):
            raise ValueError("mode: off, draft or send")
        if mode != "off" and not self.cfg.get("enabled"):
            raise ValueError("turn the station agent on first (Settings → Station agent)")
        if mode == "send":                                      # only a radio whose Transmit switch is on (station/ptt.py)
            ok, why = self.st.ptt.allowed(rid)
            if not ok:
                raise ValueError(f"Send needs this radio's transmitter: {why}")
        with self.lock:
            self.cfg.setdefault("radios", {})[rid] = mode
            self._save()
        return self.view()

    def mode(self, rid) -> str:
        if not self.cfg.get("enabled"):
            return "off"
        return (self.cfg.get("radios") or {}).get(rid, "off")

    def view(self) -> dict:
        keys = {p: secrets.status(self.st.root, k)["set"] for p, (k, _) in self.providers.items()}
        return {"settings": {k: v for k, v in self.cfg.items()}, "call": self.st.me, "default_name": DEFAULT_NAME,
                "phonetic": phonetic(self.st.me or ""),
                "models": [{"id": m, "label": v["label"], "ready": keys.get(v["provider"], False)}
                           for m, v in MODELS.items()],
                "online": {p: o.view() for p, o in self.online.items()},
                "send": self.sender.view(),
                "topics": self.topics.view(),
                "tx_ready": {rid: self.st.ptt.allowed(rid)[0] for rid in self.st.radios},
                "pending": {rid: {**s["pending"], "queue": [dict(question=x.get("question"), asker=x.get("asker"),
                                                                 state=x.get("state")) for x in s.get("queue") or []]}
                            for rid, s in self.state.items() if s.get("pending")},
                "classes": list(PHONE), "drafts": list(self.drafts),
                "turn": {rid: self.turn(rid) for rid in self.st.radios if self.mode(rid) in ("draft", "send")},
                "mode_now": self._mode_now(keys)}

    def _mode_now(self, keys: dict) -> dict:
        """How Sidekick writes its drafts now, in plain words: with the model, or with its offline rules and why."""
        prov = MODELS.get(self.cfg.get("model") or DEFAULT_MODEL, {}).get("provider", "xai")
        if not keys.get(prov):
            return {"how": "rules", "why": "no xAI key", "say": "Offline rules: no xAI key on this PC. Sidekick checks in "
                    "when check-ins are open to it and acknowledges a call; it cannot answer questions."}
        o = self.online.get(prov)
        if o is not None and o.online is False:
            return {"how": "rules", "why": "offline", "say": f"Offline rules: xAI not reachable ({o.last_error}). "
                    "Sidekick checks in and acknowledges a call; questions wait for the operator. It tries again by itself."}
        return {"how": "model", "why": "", "say": ""}

    # ----- hearing -----
    def on_final(self, tx: dict):
        """A transmission was transcribed: draft (after DEBOUNCE_S of quiet) on a radio in Draft mode. A pending
        draft or stand-by answer on this radio is dropped: someone else spoke first."""
        rid = tx.get("radio")
        if self.mode(rid) not in ("draft", "send") or tx.get("noise") or not (tx.get("text") or "").strip():
            return
        if self.mode(rid) == "send":
            self.sender.heard(rid)                              # someone else has the floor: a countdown stops
        call = self.st.me or ""
        if call and tx.get("speaker") == call and self._net(rid, self.st.clock()).get("name"):
            st, contact = self.state.setdefault(rid, {}), self.contact_now(rid)   # the operator on the air himself
            if st.get("checked_in") != contact:                                  # (11-10: heard on another receiver)
                self._checked_in(st, contact, self.st.clock(), f"{call} heard on the air")
                self.st.bus.publish("agent", {"turn": {rid: self.turn(rid)}})
        elif call and call in (tx.get("callsigns") or []) and acknowledged(tx.get("text") or ""):
            st, contact = self.state.setdefault(rid, {}), self.contact_now(rid)   # net control logged this station: at
            if st.get("checked_in") != contact:                                  # once, even if the next station keys
                self._checked_in(st, contact, self.st.clock(), "net control acknowledged " + call)   # inside 1.2 s
                self.st.bus.publish("agent", {"turn": {rid: self.turn(rid)}})
        self._later(rid, DEBOUNCE_S, tx.get("id"))
        if self.speculate:                                      # the model starts now; the draft after the quiet
            threading.Thread(target=self._safe_draft, args=(rid, tx.get("id")), kwargs={"speculate": True},
                             daemon=True, name=f"spec-{rid}").start()

    def _later(self, rid, delay, after, why=""):
        old = self.timers.pop(rid, None)
        if old:
            old.cancel()
        t = threading.Timer(delay, self._safe_draft, args=(rid, after), kwargs={"why": why})
        t.daemon = True
        self.timers[rid] = t
        t.start()

    def _safe_draft(self, rid, after, why="", speculate=False):
        try:
            self.draft(rid, after, why=why, speculate=speculate)
        except Exception as e:  # noqa: BLE001 - the agent never stops the station
            print(f"agent {rid}: {type(e).__name__}: {e}", flush=True)

    def _context(self, rid, now):
        since = (now - timedelta(minutes=CONTEXT_MIN)).isoformat(timespec="seconds")
        rows = [t for t in list(self.st.tx) if t.get("radio") == rid and t.get("status") == "final"
                and str(t.get("start") or "") >= since and not t.get("noise")][-CONTEXT_MAX:]
        return rows

    def _net(self, rid, now, rows=()):
        """The net on now: the schedule's, else the one the latest transmission was filed under."""
        try:
            n = (self.st._current_by_radio(now) or {}).get(rid) or {}
        except Exception:  # noqa: BLE001
            n = {}
        if not n and rows and isinstance(rows[-1].get("net"), dict) and rows[-1]["net"].get("key"):
            n = dict(rows[-1]["net"])
        return n

    def _local_names(self, net_name):
        try:
            from . import localnames
            entries = localnames.load(self.st.data)
            return localnames, entries, localnames.rules(entries, net_name)
        except Exception:  # noqa: BLE001 - local names are a help, never a failure
            return None, [], []

    def _rate_ok(self) -> bool:
        mono = time.monotonic()
        while self.calls and mono - self.calls[0] > 3600:
            self.calls.popleft()
        if len(self.calls) >= self.per_hour:
            return False
        self.calls.append(mono)
        return True

    def draft(self, rid, after=None, asked=False, why="", speculate=False) -> dict | None:
        """speculate=True (on_final, before the DEBOUNCE_S of quiet): build the same prompt and, if the model would be
        asked, start asking it now; nothing is drafted, logged or changed. The debounced draft uses that answer only
        when its own prompt, schema, models and context are identical (else it asks afresh): the model's time overlaps
        the courtesy wait instead of following it."""
        now = self.st.clock()
        rows = self._context(rid, now)
        if not rows and not asked:
            return None
        r = self.st.radios.get(rid) or {}
        net = self._net(rid, now, rows)
        call, name = self.st.me or "N0CALL", self.name
        op = self.cfg.get("operator") or "the operator"
        st = self.state.setdefault(rid, {})
        contact = (net.get("key") or f"{rid}@{now:%Y-%m-%d}")
        last_id = st.get("last_id")
        id_due = not last_id or now - last_id >= timedelta(minutes=ID_EVERY_MIN)
        first = st.get("disclosed") != contact
        recent = " ".join(t.get("text", "") for t in rows[-3:])
        emergency = ((bool(net.get("emergency")) and bool(re.search(r"\b(emergency|priority|break break)\b", recent, re.I)))
                     or bool(BREAK.search(recent)))           # 97.101(c): on every net, flagged or not
        busy = bool(r.get("carrier"))                      # a signal on the frequency right now
        ln, entries, lrules = self._local_names(net.get("name"))
        invited, inv_why = checkin_invited(rows, self.cfg.get("region") or "", net.get("name") or "")
        if invited and net.get("checkins") == "none":
            invited, inv_why = False, "this net is set to never check in (Nets → the net → Sidekick)"
        lastrow0 = rows[-1] if rows else {}
        if (not speculate and rows and call in (lastrow0.get("callsigns") or []) and acknowledged(lastrow0.get("text", ""))
                and st.get("checked_in") != contact):            # net control logged this station: checked in
            self._checked_in(st, contact, now, "net control acknowledged " + call)
        lines = []
        for t in rows:
            who = t.get("speaker") or (t.get("callsigns") or ["?"])[0]
            text = (t.get("text") or "").strip()
            if lrules:
                text, _ = ln.apply(text, lrules)
            lines.append(f"[{str(t.get('start', ''))[11:19]}] {who}: {text}")
        named = ln.terms_in(lines, entries) if ln else []
        try:
            utc = now.astimezone(timezone.utc)               # a naive clock is the PC's local time
        except (OSError, ValueError, OverflowError):
            utc = None
        user = "\n".join([
            f"This station: {self.cfg.get('station') or 'a fixed home station on land (not mobile, not maritime), no traffic to pass'}.",
            f"Frequency: {r.get('freq_mhz') or '?'} MHz {r.get('mode') or ''}",
            f"Local time: {now:%A %H:%M}" + (f" (UTC {utc:%H:%M})" if utc else ""),
            f"Net: {net.get('name') or 'none scheduled now'}" + (f", net control {net['ncs']}" if net.get("ncs") else ""),
            *([f"Local names heard: " + "; ".join(f"{e['name']} = {e['meaning'] or e['name']}" +
                                                  (f" ({e['web']})" if e["web"] else "") for e in named)] if named else []),
            f"The operator's own check-in line, for FACTS ONLY (never say it, never speak as {op}): "
            f"{self.cfg.get('checkin') or '(none)'}",
            f"{call} has {'already' if st.get('checked_in') == contact else 'not yet'} checked in to this net.",
            f"Check-ins open to {call} right now (the station's reading of net control): "
            + ("YES - " + inv_why if invited else "NO" + (f" - {inv_why}" if inv_why else
               " - no general call for check-ins heard") + ". Do not check in."),
            f"Your first transmission in this contact: {'yes - disclose' if first else 'no'}.",
            f"Minutes since {call} last identified: {'never' if not last_id else int((now - last_id).total_seconds() // 60)}.",
            *self._pending_lines(st.get("pending"), why, rows, now, st.get("queue") or []),
            "Transcript (oldest first; speech recognition, may contain errors):",
            *(lines or ["(nothing heard yet)"]),
            "Should " + call + " transmit now? Answer with the JSON.",
        ])
        system = SYSTEM.format(name=name, call=call, op=op, kinds="/".join(KINDS))
        lastrow = rows[-1] if rows else {}
        addressed = bool(rows) and (call in (lastrow.get("callsigns") or []) or name_in(lastrow.get("text", ""), name))
        if addressed and lastrow.get("speaker") == call:
            addressed = False                                  # this station's own transmission, heard back
        if addressed and acknowledged(lastrow.get("text", "")) and not name_in(lastrow.get("text", ""), name):
            addressed = False                                  # "W9XYZ, thank you": logged, not asked anything
        ctx = {"call": call, "name": name, "op": op, "first": first, "id_due": id_due, "emergency": emergency,
               "busy": busy, "freq_mhz": r.get("freq_mhz") or lastrow.get("freq_mhz"), "mode": r.get("mode"),
               "license_class": self.cfg.get("license_class"), "addressed": addressed, "asked": asked,
               "in_net": bool(net.get("name")), "trigger": why,
               "queue_full": len(st.get("queue") or []) >= QUEUE_MAX, "last_tx": lastrow.get("id"),
               "asked_tx": {x.get("tx") for x in [st.get("pending") or {}, *(st.get("queue") or [])] if x.get("tx")},
               "roll_call": bool(rows) and bool(ROLL_CALL.search(lastrow.get("text", ""))),
               "invited": invited}
        main_model = self.cfg.get("model") or DEFAULT_MODEL
        models = [main_model] + list(self.cfg.get("compare") or [])
        results = {}
        allowed, gate_why = allowed_kinds(ctx, st.get("checked_in") == contact, st.get("pending"), lastrow.get("text", ""))
        if allowed == ["none"] and inv_why and not invited and not emergency:
            gate_why = inv_why                                 # say whom net control called, not just "not called"
        if st.get("muted") == contact and not asked:
            allowed, gate_why = ["none"], "muted for this net"
        spec = None
        tm = None if asked or st.get("muted") == contact else self._topic_move(rid, ctx, lastrow.get("text", ""))
        if tm:
            # the operator's approved words, said as written (no model): a prepared topic on the net
            models, rate_ok = [], False
            main = self._finish(tm, ctx | {"present_words": self._present_words(rid)}, "station", 0.0, addressed)
            main.update(topic=tm["topic"], seg=tm["seg"])
        elif allowed == ["none"]:
            # not this station's turn (not called, no check-ins open to it, no answer waiting): silent at once, no model
            models, rate_ok = [], False
            main = self._finish({"speak": False, "kind": "none", "say": "", "wait_s": 2.0, "why": gate_why}, ctx,
                                "station", 0.0, addressed)
        elif ("answer" in allowed and why in ("pause", "answer ready") and not ctx["in_net"] and not asked
              and self._quiet_for(rows, now) < PAUSE_S - 0.05):
            models, rate_ok = [], False                        # ready mid-conversation: wait for the lull
            main = self._finish({"speak": False, "kind": "none", "say": "", "wait_s": 2.0,
                                 "why": "answer ready: waiting for a pause"}, ctx, "station", 0.0, addressed)
        elif "answer" in allowed and why in ("pause", "answer ready") and not ctx["in_net"] and not asked:
            # outside a net, a lull with an answer ready: the station's own move, at once (owner 11-12), no model
            models, rate_ok = [], False
            main = self._finish(rules_draft(rows, ctx, st.get("checked_in") == contact, st.get("pending")), ctx,
                                "station", 0.0, addressed)
        else:
            schema = dict(SCHEMA, properties=dict(SCHEMA["properties"], kind={"type": "string", "enum": allowed}))
            user += "\nYour possible moves now: " + ", ".join(allowed) + "."
            key = json.dumps([system, user, schema, models, ctx], sort_keys=True, default=str)
            if speculate:
                if models and self._rate_ok():
                    self._start_spec(rid, key, models, system, user, ctx, addressed, schema)
                return None
            spec = self._take_spec(rid, key)
            rate_ok = asked or spec is not None or self._rate_ok()
        if speculate:
            return None                                    # no model would be asked: nothing to start early
        if models and rate_ok and spec:
            spec["done"].wait(MODEL_TIMEOUT_S + 2)
            for m, r in list(spec["results"].items()):
                if not r.get("error"):                     # the model ran from the end of the transmission
                    r["silence_s"] = round(max(r["wait_s"], DEBOUNCE_S, r.get("latency_s") or 0), 1)
                    r["speculative"] = True
                results[m] = r
        elif models and rate_ok:
            def run(m):
                results[m] = self._ask(m, system, user, ctx, addressed, schema)
            threads = [threading.Thread(target=run, args=(m,), daemon=True) for m in models]
            for th in threads:
                th.start()
            for th in threads:
                th.join(MODEL_TIMEOUT_S + 2)
        if models:
            main = results.get(main_model) or {"model": main_model, "error": "over the hourly model limit"
                                              if not rate_ok else "no answer"}
        if main.get("error"):                              # offline, no key, over the limit: the rules answer now
            fb = main["error"]
            main = self._finish(rules_draft(rows, ctx, st.get("checked_in") == contact, st.get("pending")), ctx, RULES,
                                0.0, addressed)
            main["fallback"] = fb
        main = self._protocol(main, st.get("pending"), ctx)
        d = {"id": uuid.uuid4().hex[:10], "radio": rid, "at": now.isoformat(timespec="seconds"), "after": after,
             "invited": bool(invited), "invite_why": inv_why, "contact": contact,
             "heard": lines[-1] if lines else "", "net": net.get("name"), "addressed": bool(addressed),
             "asked": asked, "trigger": why or ("asked" if asked else "heard"), **main,
             "alts": [results[m] | {"model": m} for m in models[1:] if m in results]}
        if d.get("speak") and d.get("ok"):
            if self.mode(rid) == "send" and not asked:
                # Send mode: the ID clock, the disclosure, the check-in and the prepared answer move on only when it
                # is really said (Sender -> sent()); a stopped or held send changes nothing
                self._to_send[d["id"]] = (rid, d, rows, now, ctx, contact, lines, user)
                d["send"] = self.sender.offer(rid, d, ctx)
                if d["send"]["state"] != "counting":
                    self._to_send.pop(d["id"], None)
            else:
                self._as_said(rid, st, d, rows, now, ctx, contact, lines, user)
        with self.lock:
            self.drafts.append(d)
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(d) + "\n")
            except OSError:
                pass
        keys = {p: secrets.status(self.st.root, k)["set"] for p, (k, _) in self.providers.items()}
        self.st.bus.publish("agent", {"draft": d, "turn": {rid: self.turn(rid)}, "mode_now": self._mode_now(keys)})
        pend = st.get("pending") or {}
        if (not ctx["in_net"] and pend.get("state") in ("ready", "failed") and not d.get("speak") and why != "pause"
                and not asked and self.mode(rid) in ("draft", "send")):
            self._later(rid, PAUSE_S - DEBOUNCE_S, None, why="pause")   # a conversation: the answer at the next lull
        return d

    def _as_said(self, rid, st, d, rows, now, ctx, contact, lines, user, how="drafted a check-in"):
        if _has_call(d.get("say", ""), ctx["call"]):
            st["last_id"] = now                                  # Draft mode: as if sent, keeps the clock honest
        if re.search(r"\bautomat\w*\b", d.get("say", ""), re.I) and re.search(r"control operator", d.get("say", ""), re.I):
            st["disclosed"] = contact
        if d.get("kind") == "check_in":
            self._checked_in(st, contact, now, how)
        if d.get("kind") == "present":
            self.topics.said(rid, d.get("topic"), d.get("seg"))
            self.st.bus.publish("agent", {"topics": self.topics.view()})
        self._advance(rid, st, d, rows, now, ctx, contact, lines, user)
        self._save_state()

    def sent(self, rid, rec):
        """Sender: the draft was said on the air."""
        args = self._to_send.pop(rec.get("draft"), None)
        if not args:                                            # a prepared part the operator sent by hand
            st = self.state.setdefault(rid, {})
            if _has_call(rec.get("say", ""), self.st.me or ""):
                st["last_id"] = self.st.clock()
            m = re.fullmatch(r"prep-(\w+)-(\d+)", str(rec.get("draft") or ""))
            if m:
                self.topics.said(rid, m.group(1), int(m.group(2)))
                self.st.bus.publish("agent", {"topics": self.topics.view()})
            self._save_state()
            return
        _, d, rows, _, ctx, contact, lines, user = args
        st = self.state.setdefault(rid, {})
        self._as_said(rid, st, d, rows, self.st.clock(), ctx, contact, lines, user, how="checked in on the air")
        self.st.bus.publish("agent", {"turn": {rid: self.turn(rid)}})

    def library(self):
        """Sidekick's knowledge library (station/knowledge.py): this project's docs + the operator's own material."""
        with self._lib_lock:                                # several answers think at once: one library
            if self._lib is None:
                from .knowledge import Library
                self._lib = Library(self.st.data, self.st.root)
        return self._lib

    def _present_words(self, rid) -> int:
        from .topics import WORDS_PER_S
        t = self.st.ptt.cfg(rid) or {}
        return int((min(int(t.get("max_s") or 60), 180) - 3) * WORDS_PER_S)

    def _topic_move(self, rid, ctx, text) -> dict | None:
        """An approved topic armed on this radio, and the station asked to present it (or to continue)."""
        from .topics import CONTINUE, PRESENT
        nxt = self.topics.next_segment(rid)
        if not nxt or not ctx.get("addressed") or ctx.get("emergency"):
            return None
        t, i, words = nxt
        if not (PRESENT.search(text or "") or (i > 0 and CONTINUE.search(text or ""))):
            return None
        if i == 0 and ctx.get("first") and not re.search(r"\bautomat", words, re.I):
            words = f"This is {ctx['call']}, {ctx['name']}, an automated assistant; {ctx['op']} is control operator. " \
                    + words
        return {"speak": True, "kind": "present", "say": words, "wait_s": 1.5, "question": "",
                "why": f"presenting \u201c{t['title']}\u201d, part {i + 1} of {len(t['segments'])}",
                "topic": t["id"], "seg": i}

    @staticmethod
    def _quiet_for(rows, now) -> float:
        """Seconds since the last transmission heard ended."""
        try:
            last = rows[-1]
            end = datetime.fromisoformat(last["start"]) + timedelta(seconds=float(last.get("duration_s") or 0))
            return max(0.0, (now - end).total_seconds())
        except (IndexError, KeyError, ValueError, TypeError):
            return 1e9

    def _checked_in(self, st, contact, now, how):
        st["checked_in"], st["checked_in_at"], st["checked_in_how"] = contact, now.isoformat(timespec="seconds"), how
        self._save_state()

    def contact_now(self, rid) -> str:
        now = self.st.clock()
        net = self._net(rid, now, self._context(rid, now))
        return net.get("key") or f"{rid}@{now:%Y-%m-%d}"

    def turn(self, rid) -> dict:
        """The radio's turn state for the dashboard (YOUR TURN chip)."""
        st, contact = self.state.get(rid) or {}, self.contact_now(rid)
        L = [d for d in self.drafts if d.get("radio") == rid]
        last = L[-1] if L else None
        speaking = [d for d in L if d.get("speak")]
        out = {"checked_in": st.get("checked_in") == contact, "checked_in_at": st.get("checked_in_at")
               if st.get("checked_in") == contact else None, "checked_in_how": st.get("checked_in_how")
               if st.get("checked_in") == contact else None, "muted": st.get("muted") == contact,
               "silent": sum(1 for d in L if not d.get("speak")), "last_speak": speaking[-1] if speaking else None}
        if out["muted"]:
            out.update(state="muted", say="Muted for this net")
        elif last and last.get("speak") and last.get("addressed"):
            out.update(state="called", say="CALLED: " + (last.get("heard") or "")[-90:])
        elif last and last.get("speak") and last.get("kind") in ("ready", "answer"):
            out.update(state="answer", say="Answer ready")
        elif last and last.get("invited") and not out["checked_in"]:
            out.update(state="open", say="Check-ins open to you")
        elif out["checked_in"]:
            out.update(state="in", say="Checked in")
        else:
            out.update(state="listening", say="Listening" + (f": {last['invite_why']}" if last and last.get(
                "invite_why") and not last.get("invited") else ""))
        return out

    def mark(self, rid, what: str) -> dict:
        """The operator: "checked_in" (I checked in myself), "not_checked_in", "mute", "unmute" for this net."""
        if rid not in self.st.radios:
            raise ValueError(f"no radio called {rid!r}")
        st, contact, now = self.state.setdefault(rid, {}), self.contact_now(rid), self.st.clock()
        if what == "checked_in":
            self._checked_in(st, contact, now, "the operator checked in")
        elif what == "not_checked_in":
            st.pop("checked_in", None); st.pop("checked_in_at", None); st.pop("checked_in_how", None)
        elif what == "mute":
            st["muted"] = contact
        elif what == "unmute":
            st.pop("muted", None)
        else:
            raise ValueError("checked_in, not_checked_in, mute or unmute")
        self._save_state()
        v = self.view()
        self.st.bus.publish("agent", {"turn": {rid: self.turn(rid)}})
        return v

    # ----- the prepared answer (two speeds) -----
    def _pending_lines(self, p, why, rows, now, queue=()) -> list[str]:
        if not p:
            return []
        later = [f"Also waiting, after it: \"{x.get('question')}\" from {x.get('asker')} ({x.get('state')})." for x in queue]
        q, who, at = p.get("question") or "?", p.get("asker") or "a station", p.get("at", "")[11:16]
        state = p.get("state")
        one = (f"Up to {QUEUE_MAX + 1} questions are worked on at once; they are answered in the order asked. "
               "Another question that needs thinking: kind working again (it joins the list).")
        if state == "working":
            return later + [one, f"Your prepared answer: WORKING on \"{q}\" (asked by {who} at {at}). It is not ready: do not "
                    f"mention it, carry on as usual."]
        if state == "ready":
            out = [one, f"Your prepared answer: READY for \"{q}\" (asked by {who} at {at}). Announce it (kind ready) only "
                   f"at the right opening; otherwise carry on as usual."]
            if why == "answer ready" and rows:
                last = rows[-1]
                try:
                    end = datetime.fromisoformat(last["start"]) + timedelta(seconds=float(last.get("duration_s") or 0))
                    out.append(f"(It just became ready. The last transmission ended {max(0, int((now - end).total_seconds()))} "
                               f"s ago.)")
                except (KeyError, ValueError):
                    pass
            return out + later
        if state == "announced":
            return [one, f"Your prepared answer: ANNOUNCED at {p.get('announced_at', '')[11:16]} (for \"{q}\" from {who}). "
                    f"If you are now told to go ahead, kind answer. If told to wait, or the net moved on, stay silent "
                    f"until given the floor."] + later
        if state == "failed":
            return [f"Your prepared answer: FAILED for \"{q}\" (asked by {who} at {at}). At the next opening say "
                    f"briefly that you could not get it and the control operator will follow up (kind reply)."] + later
        return []

    def _protocol(self, d: dict, p: dict | None, ctx: dict) -> dict:
        """The prepared-answer steps checked in code: no announcing an answer that is not ready, and the answer read
        is the prepared words (with the station's ID), never the quick model's paraphrase."""
        if not d.get("speak") or d.get("kind") not in ("ready", "answer", "working", "check_in", "reply", "acknowledge",
                                                         "say_again", "stand_by"):
            return d
        d = dict(d)
        p = p or {}
        if d["kind"] in ("reply", "acknowledge", "say_again", "stand_by", "working") and not ctx.get("addressed") \
                and not ctx.get("asked") and not (d["kind"] == "reply" and p.get("state") == "failed"):
            # 11-10 replay: after the net closed it volunteered "just checking in after the net closed, thanks for the
            # experiment". Talking to the frequency unasked is not this station's place.
            d["fcc"] = d.get("fcc", []) + [{"rule": "protocol", "ok": False,
                                            "note": "nobody called this station (by call or name)"}]
            d["ok"] = False
            return d
        if d["kind"] == "check_in" and not ctx.get("invited", True):
            # 11-10 replay: the quick model checked in twice during a net's roll call of its city stations. A check-in
            # that breaks into a roll call costs more than a late one: it needs net control's general call.
            d["fcc"] = d.get("fcc", []) + [{"rule": "protocol", "ok": False, "note":
                                            "a roll call of places or named stations, no general call for check-ins"
                                            if ctx.get("roll_call") else "no general call for check-ins heard"}]
            d["ok"] = False
            return d
        if d["kind"] == "working":
            if ctx.get("last_tx") and ctx["last_tx"] in (ctx.get("asked_tx") or ()):
                d["fcc"] = d.get("fcc", []) + [{"rule": "protocol", "ok": False,
                                                "note": "already working on the question in this transmission"}]
                d["ok"] = False
                return d
            if p.get("state") in ("working", "ready", "announced") and ctx.get("queue_full"):
                d["fcc"] = d.get("fcc", []) + [{"rule": "protocol", "ok": False, "note":
                                                f"{QUEUE_MAX + 1} questions already waiting (first: {p.get('question')})"}]
                d["ok"] = False
            return d
        if d["kind"] == "ready" and p.get("state") not in ("ready", "announced"):
            d["fcc"] = d.get("fcc", []) + [{"rule": "protocol", "ok": False, "note": "no prepared answer is ready"}]
            d["ok"] = False
            return d
        if d["kind"] == "answer":
            if p.get("state") not in ("ready", "announced") or not p.get("answer"):
                d["fcc"] = d.get("fcc", []) + [{"rule": "protocol", "ok": False,
                                                "note": "no prepared answer to read"}]
                d["ok"] = False
                return d
            say = p["answer"].strip()
            if p.get("state") == "ready" and not ctx.get("in_net"):     # given at a pause: say whom it is for
                who = f"for {p.get('asker') or 'the earlier question'}: {say}"
                say = (f"This is {ctx['call']}, {ctx['name']}, an automated assistant, {ctx['op']} is control "
                       f"operator, {who}" if ctx.get("first") else f"This is {ctx['call']} {ctx['name']}, {who}")
            if not _has_call(say, ctx["call"]):
                say = f"{say.rstrip('.')}. {ctx['call']}."
            d["say"] = say
            d["fcc"] = fcc_check(d, ctx)
            d["ok"] = all(c["ok"] for c in d["fcc"])
        return d

    def _advance(self, rid, st, d, rows, now, ctx, contact, lines, user):
        """A draft the station would have sent moves the prepared answer along."""
        p, kind = st.get("pending"), d.get("kind")
        last = rows[-1] if rows else {}
        if kind == "working":
            asker = last.get("speaker") or next((c for c in last.get("callsigns") or [] if c != ctx["call"]), None)
            q = {"id": uuid.uuid4().hex[:8], "state": "working", "question": d.get("question") or last.get("text", ""),
                 "tx": last.get("id"),
                 "asker": asker or "a station", "at": now.isoformat(timespec="seconds"), "radio": rid,
                 "model": self.cfg.get("deep_model") or DEEP_MODEL, "contact": contact}
            if p and p.get("state") in ("working", "ready", "announced"):
                st.setdefault("queue", []).append(q)            # answered after the one before it, in order
            else:
                st["pending"] = p = q
            th = threading.Thread(target=self._think_safe, args=(rid, dict(q), list(lines), ctx), daemon=True)
            th.start()
            self._thinking = th                                 # (tests join it)
        elif kind == "ready" and p:
            p.update(state="announced", announced_at=now.isoformat(timespec="seconds"))
        elif kind == "answer" and p:
            self._next_pending(rid, st)
        elif kind == "reply" and p and p.get("state") == "failed":
            self._next_pending(rid, st)
        cur = st.get("pending")
        if kind in ("working", "ready", "answer") or (p and cur is not p):
            self._publish_pending(rid, st)

    def _next_pending(self, rid, st):
        """The answered question is done: the next one asked moves up (its own thinking already running or done)."""
        st.pop("pending", None)
        q = st.get("queue") or []
        if q:
            st["pending"] = q.pop(0)
            if st["pending"].get("state") in ("ready", "failed") and self.mode(rid) in ("draft", "send"):
                self._later(rid, PAUSE_S, None, why="pause")    # its place comes at the next opening
        self._save_state()

    def _publish_pending(self, rid, st):
        cur = st.get("pending")
        self.st.bus.publish("agent", {"pending": {"radio": rid, **(cur or {"state": "none"}),
                                                  "queue": [dict(question=x.get("question"), asker=x.get("asker"),
                                                                 state=x.get("state")) for x in st.get("queue") or []]}})

    def _think_safe(self, rid, p, lines, ctx):
        try:
            self._think(rid, p, lines, ctx)
        except Exception as e:  # noqa: BLE001
            self._thought(rid, p["id"], {"state": "failed", "error": f"{type(e).__name__}: {str(e)[:120]}"})

    def _think(self, rid, p, lines, ctx):
        model = p["model"] if p["model"] in MODELS else DEEP_MODEL
        prov = MODELS[model]["provider"]
        keyname, fn = self.providers[prov]
        key = secrets.read(self.st.root, keyname)
        if not key:
            return self._thought(rid, p["id"], {"state": "failed", "error": f"no {keyname}"})
        on = self.online.setdefault(prov, Online(prov))
        if not on.should_try():
            return self._thought(rid, p["id"], {"state": "failed", "error": f"{prov} offline"})
        system = DEEP_SYSTEM.format(name=ctx["name"], call=ctx["call"], op=ctx["op"], asker=p["asker"],
                                    question=p["question"], words=ANSWER_WORDS - 5)
        r = self.st.radios.get(rid) or {}
        extra = []
        tools = None
        if fn is call_xai:                                      # xAI: the model asks for the library / a path itself
            from .toolbox import Toolbox
            tools = Toolbox(self, rid)
            system += TOOLS_NOTE
        try:
            if tools:
                raise LookupError                               # (no prefetched context: it has the tools)
            lib = self.library().context_for(p["question"], 6000)
            if lib:
                extra = ["Reference material from the station's library (the operator's own documents and this "
                         "station's manual; authoritative where it applies; do not read the labels aloud):", lib]
        except Exception:  # noqa: BLE001 - the library is a help, never a failure
            pass
        try:
            if tools:
                raise LookupError
            from .topics import path_facts, sites_named
            named = sites_named(p["question"], self.topics.sites)
            if len(named) >= 2:
                extra += path_facts(named[:4], float(r.get("freq_mhz") or 146.0) if 30 <= float(r.get("freq_mhz") or 146)
                                    <= 1300 else 146.0)
        except Exception:  # noqa: BLE001
            pass
        user = "\n".join([*facts(), *extra,
                          f"Station: {self.cfg.get('station') or 'a fixed home station'}; frequency "
                          f"{r.get('freq_mhz') or ctx.get('freq_mhz') or '?'} MHz; local time "
                          f"{self.st.clock():%A %Y-%m-%d %H:%M}.",
                          "What was said before the question (speech recognition, may contain errors):", *lines[-16:],
                          f"The question to answer: {p['question']} (asked by {p['asker']})."])
        t = time.monotonic()
        try:
            if tools:
                out, usage = call_xai_tools(key, model, system, user, tools, timeout=DEEP_TIMEOUT_S,
                                            schema=THINK_SCHEMA, max_tokens=8000, name="answer")
            else:
                out, usage = fn(key, model, system, user, timeout=DEEP_TIMEOUT_S, schema=THINK_SCHEMA,
                                max_tokens=8000, name="answer")
        except Exception as e:  # noqa: BLE001
            if is_network_error(e):
                on.failed(e)
            else:
                on.ok()
            return self._thought(rid, p["id"], {"state": "failed", "error": f"{type(e).__name__}: {str(e)[:120]}",
                                                "took_s": round(time.monotonic() - t, 1)})
        on.ok()
        answer = re.sub(r"\s+", " ", str(out.get("answer") or "")).strip()
        words = answer.split()
        if len(words) > ANSWER_WORDS:                           # keep whole sentences inside one transmission
            cut = " ".join(words[:ANSWER_WORDS])
            answer = cut[:cut.rfind(".") + 1] if "." in cut else cut
        check = fcc_check({"speak": True, "kind": "answer", "say": f"{answer} {ctx['call']}"},
                          ctx | {"first": False, "id_due": False, "busy": False, "emergency": False})
        broke = [c for c in check if not c["ok"]]
        upd = {"took_s": round(time.monotonic() - t, 1), "usage": usage, "confident": bool(out.get("confident")),
               "why": str(out.get("why") or "")[:200]}
        if not answer or broke:
            upd |= {"state": "failed", "error": "empty answer" if not answer else
                    "the answer broke " + ", ".join(c["rule"] for c in broke), "answer_rejected": answer}
        else:
            upd |= {"state": "ready", "answer": answer, "ready_at": self.st.clock().isoformat(timespec="seconds")}
        self._thought(rid, p["id"], upd)

    def _thought(self, rid, pid, upd):
        st = self.state.setdefault(rid, {})
        p = st.get("pending")
        if not p or p.get("id") != pid:
            q = next((x for x in st.get("queue") or [] if x.get("id") == pid), None)
            if q:
                q.update(upd)                                   # waiting its turn: kept, said after the one before
                self._save_state()
                self._publish_pending(rid, st)
            return                                              # else superseded (a restart)
        p.update(upd)
        self._save_state()
        self._publish_pending(rid, st)
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"pending": {"radio": rid, **p}}) + "\n")
        except OSError:
            pass
        if self.mode(rid) in ("draft", "send"):                           # its place may already be open: let the quick model see
            t = self.timers.get(rid)
            if not (t and t.is_alive()):
                self._later(rid, 0.2, None, why="answer ready" if p["state"] == "ready" else "answer failed")

    def _start_spec(self, rid, key, models, system, user, ctx, addressed, schema):
        spec = {"key": key, "results": {}, "done": threading.Event()}
        with self.lock:
            self._spec[rid] = spec                              # a newer transmission replaces an older guess

        def run():
            def one(m):
                spec["results"][m] = self._ask(m, system, user, ctx, addressed, schema)
            ths = [threading.Thread(target=one, args=(m,), daemon=True) for m in models]
            for th in ths:
                th.start()
            for th in ths:
                th.join(MODEL_TIMEOUT_S + 2)
            spec["done"].set()
        threading.Thread(target=run, daemon=True, name=f"spec-ask-{rid}").start()

    def _take_spec(self, rid, key):
        """The speculative answer for exactly this prompt, else None (a stale guess is dropped either way)."""
        with self.lock:
            spec = self._spec.pop(rid, None)
        return spec if spec and spec["key"] == key else None

    def _ask(self, model, system, user, ctx, addressed, schema=SCHEMA) -> dict:
        prov = MODELS[model]["provider"]
        keyname, fn = self.providers[prov]
        key = secrets.read(self.st.root, keyname)
        if not key:
            return {"model": model, "error": f"no {keyname} set (Settings → Station agent)"}
        on = self.online.setdefault(prov, Online(prov))
        if not on.should_try():
            return {"model": model, "error": f"{prov} offline ({on.last_error}); retrying in "
                                             f"{on.view()['retry_in_s']} s"}
        t = time.monotonic()
        try:
            out, usage = fn(key, model, system, user, schema=schema)
        except Exception as e:  # noqa: BLE001
            lat = round(time.monotonic() - t, 2)
            if is_network_error(e):
                on.failed(e)
                return {"model": model, "error": f"{prov} unreachable: {type(e).__name__}", "latency_s": lat}
            on.ok()
            if isinstance(e, urllib.error.HTTPError):
                return {"model": model, "error": f"{prov} answered {e.code}", "latency_s": lat}
            return {"model": model, "error": f"{type(e).__name__}: {str(e)[:120]}", "latency_s": lat}
        on.ok()
        d = self._finish(out, ctx, model, round(time.monotonic() - t, 2), addressed)
        d["usage"] = usage
        return d

    def _finish(self, out, ctx, model, lat, addressed) -> dict:
        d = {"model": model, "latency_s": lat, "speak": bool(out.get("speak")),
             "kind": out.get("kind") if out.get("kind") in STATION_KINDS else "none",
             "say": str(out.get("say") or "")[:600 if out.get("kind") != "present" else 3000],
             "wait_s": max(0.5, min(4.0, float(out.get("wait_s") or 2.0))), "why": str(out.get("why") or "")[:200],
             "addressed": bool(addressed), "question": str(out.get("question") or "")[:300]}
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

    def topics_post(self, body: dict) -> dict:
        """{"action": save|prepare|approve|unapprove|arm|remove|sites, ...}"""
        a = str(body.get("action") or "")
        T = self.topics
        if a == "list":
            pass
        elif a == "sites":
            T.set_sites(str(body.get("text") or ""))
        elif a == "save":
            body = dict(body)
            body.pop("action", None)
            T.upsert(body)
        elif a in ("approve", "unapprove"):
            T.approve(str(body.get("id")), a == "approve")
        elif a == "arm":
            if body.get("radio") not in self.st.radios:
                raise ValueError("no such radio")
            T.arm(body["radio"], body.get("id") or None)
        elif a == "remove":
            T.remove(str(body.get("id")))
        elif a == "prepare":
            self._prepare(str(body.get("id")))
        elif a == "save_say":                                   # from the chat: words the operator reviewed
            from .topics import segment
            text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()
            if not text:
                raise ValueError("nothing to save")
            call = self.st.me or "N0CALL"
            segs = segment(text, self._present_words(next(iter(self.st.radios), "")))
            if not _has_call(segs[-1], call):
                segs[-1] = segs[-1].rstrip(". ") + f". {call}."
            t = T.upsert({"title": str(body.get("title") or text[:50]), "request": "(saved from the chat)",
                          "segments": segs, "minutes": 1})
            if body.get("approve"):
                T.approve(t["id"], True)
        elif a == "send":                                       # the operator sends one prepared part now
            rid, tid, i = str(body.get("radio") or ""), str(body.get("id") or ""), int(body.get("seg") or 0)
            if self.mode(rid) != "send":
                raise ValueError("switch this radio's Sidekick to Send first (it needs the radio's Transmit switch)")
            t = T.get(tid)
            from .topics import fingerprint
            if t.get("approved") != fingerprint(t.get("segments") or []):
                raise ValueError("approve it first: the words you approve are the words it says")
            if not 0 <= i < len(t["segments"]):
                raise ValueError("no such part")
            self.sender.presence()                              # the operator's own click
            r = self.st.radios.get(rid) or {}
            words = t["segments"][i]
            if i == 0 and not re.search(r"\bautomat", words, re.I):     # 97.113-style disclosure, as on the air
                words = (f"This is {self.st.me or 'N0CALL'}, {self.name}, an automated assistant; "
                         f"{self.cfg.get('operator') or 'the operator'} is control operator. " + words)
            d = {"id": f"prep-{tid}-{i}", "kind": "present", "say": words, "topic": tid, "seg": i}
            T.armed.setdefault(rid, {"topic": tid, "next": i})
            return {"send": self.sender.offer(rid, d, {"freq_mhz": r.get("freq_mhz"), "emergency": False}),
                    "topics": T.view()}
        else:
            raise ValueError("unknown topics action")
        return {"topics": T.view()}

    def _prepare(self, tid):
        """Write a topic's script with the thinking model (a minute or two): in the background, the card follows."""
        t = self.topics.get(tid)
        model = self.cfg.get("deep_model") or DEEP_MODEL
        prov = MODELS.get(model, MODELS[DEEP_MODEL])["provider"]
        keyname, fn = self.providers[prov]
        key = secrets.read(self.st.root, keyname)
        if not key:
            raise ValueError(f"preparing a topic needs the thinking model: set the {keyname} (Settings → Station agent)")
        t["preparing"] = True
        self.st.bus.publish("agent", {"topics": self.topics.view()})
        from .topics import TOPIC_SCHEMA
        ctx = {"call": self.st.me or "N0CALL", "name": self.name, "op": self.cfg.get("operator") or "the operator"}
        max_s = min((self.st.ptt.cfg(rid) or {}).get("max_s", 60) for rid in self.st.radios) if self.st.radios else 60

        def ask(system, user):
            out, _ = fn(key, model, system, user, timeout=240, schema=TOPIC_SCHEMA, max_tokens=12000, name="topic")
            return out

        def run():
            try:
                r = self.topics.prepare(tid, ask, self.library(), ctx, max_s=min(max_s, 60))
                r.pop("preparing", None)
                self.topics.get(tid).pop("error", None)
                self.topics.get(tid)["sources"] = r.get("sources") or []
            except Exception as e:  # noqa: BLE001
                try:
                    self.topics.get(tid)["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                except LookupError:
                    pass
            try:
                self.topics.get(tid).pop("preparing", None)
                self.topics._save()
            except LookupError:
                pass
            self.st.bus.publish("agent", {"topics": self.topics.view()})
        th = threading.Thread(target=run, daemon=True, name="topic")
        th.start()
        self._preparing = th

    def library_post(self, body: dict) -> dict:
        """{"action": note|url|file|remove|refresh|search, ...}"""
        a, L = str(body.get("action") or ""), self.library()
        if a == "note":
            L.add_note(str(body.get("title") or ""), str(body.get("text") or ""))
        elif a == "url":
            L.add_url(str(body.get("url") or ""))
        elif a == "file":
            import base64
            L.add_file(str(body.get("name") or ""), base64.b64decode(str(body.get("data") or "")))
        elif a == "remove":
            L.remove(str(body.get("source") or ""))
        elif a == "refresh":
            L.refresh(str(body.get("source") or ""))
        elif a == "search":
            return {"hits": L.search(str(body.get("q") or ""), 6)}
        elif a != "list":
            raise ValueError("unknown library action")
        return {"library": L.list()}

    def post(self, path: str, body: dict) -> dict:
        """POST /api/agent/settings, /api/agent/radio/<rid> {"mode"}, /api/agent/ask/<rid> (draft now),
        /api/agent/rate/<draft id> {"good", "note"}."""
        m = re.fullmatch(r"/api/agent/(settings|radio|ask|rate|mark|stop|presence|txtest|topics|library|chat)(?:/([\w-]+))?", path)
        if not m:
            raise ValueError("unknown agent request")
        what, arg = m.groups()
        if what == "chat":                                      # {"radio", "message", "deep"} / {"radio", "clear"}
            rid = str(body.get("radio") or arg or "")
            if body.get("clear"):
                self.chat.clear(rid)
                return {"chat": []}
            if body.get("message"):
                self.chat.ask(rid, body["message"], bool(body.get("deep")))
            return {"chat": self.chat.thread(rid), "busy": bool(self.chat.busy.get(rid))}
        if what == "topics":
            return self.topics_post(body)
        if what == "library":
            return self.library_post(body)
        if what == "presence":                                  # the dashboard: keyboard or mouse in use
            return self.sender.presence()
        if what == "txtest":                                    # Radios… → Transmit test… (dummy load)
            if arg not in self.st.radios:
                raise ValueError(f"no radio called {arg!r}")
            return {"send": self.sender.test(arg, body)}
        if what == "stop":                                      # Esc / Stop / a click: drop it now
            return {"send": self.sender.stop(arg if arg and arg != "all" else None)}
        if what == "settings":
            return self.settings(body)
        if what == "radio":
            return self.set_radio(arg, str(body.get("mode") or "off"))
        if what == "mark":
            return self.mark(arg, str(body.get("what") or ""))
        if what == "ask":
            if not self.cfg.get("enabled"):
                raise ValueError("the station agent is off (Settings → Station agent)")
            if arg not in self.st.radios:
                raise ValueError(f"no radio called {arg!r}")
            self.draft(arg, asked=True)
            return self.view()
        return self.rate(arg, body.get("good"), body.get("note", ""))
