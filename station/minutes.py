"""Meeting minutes for a net, drafted from its transcript by rules (offline, explainable, testable).

    extract(views, ref_date, emergency=False) -> [item, ...]

Each item is a SUGGESTION tied to the transmission it came from (`tx_id`); the operator confirms, edits, closes or
dismisses it on the dashboard, and those edits are a separate layer the next extraction never overwrites
(station/history.py). Kinds:

    announcement  "Reminder: the hamfest is Saturday the 14th", "VE session ... sign up by ..."  (+ until / event date)
    event         a date or time AND something that happens then ("the club meeting is Thursday at 7 PM")
    action        someone will do something, or something is asked for ("K7ABC will send the roster",
                  "volunteers needed for ...", "can someone ...", "please email ... by Friday") (+ who, due)
    decision      "we agreed / decided / the motion passed ..."
    traffic       formal traffic or a relay handled (radiogram, message for ..., QTC); never "no traffic"
    urgent        injury, fire, flood, missing person, medical, shelter, power, roads, resource requests, a mayday
                  (+ category, people, place); questions ("any emergency traffic?") and denials ("no injuries") are not

An LLM may refine the drafts when one is configured (refine() below); it is OFF and offline by default.
"""
import re
from datetime import date, datetime, timedelta

CALL = r"[A-Z]{1,2}\d[A-Z]{1,4}|\d[A-Z]\d[A-Z]{1,3}"
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MON = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_WD = r"(?P<wd>mon|tues|wednes|thurs|fri|satur|sun)days?"
_D = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
DATE_RES = [
    re.compile(rf"\b{_MON}\s+(?:the\s+)?{_D}\b", re.I),                                  # November 14th
    re.compile(rf"\bthe\s+{_D}\s+of\s+{_MON}", re.I),                                   # the 14th of November
    re.compile(r"\b(?P<m>1[0-2]|0?[1-9])/(?P<day>[0-3]?\d)(?:/(?P<y>\d{2,4}))?\b"),       # 11/14
    re.compile(rf"\b(?P<rel>next|this|this coming|coming|on)?\s*{_WD}\b(?:,?\s+(?:the\s+)?{_D}\b)?", re.I),
    re.compile(r"\b(?P<word>today|tonight|tomorrow|tomorrow night|this evening)\b", re.I),
    re.compile(r"\bthe\s+(?P<day>[0-3]?\d)(?:st|nd|rd|th)\b", re.I),                     # the 14th
]
TIME_RES = [
    re.compile(r"\b(?P<h>1[0-2]|0?[1-9])(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap])\.?\s?m\b\.?", re.I),
    re.compile(r"\b(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)\b"),
    re.compile(r"\b(?P<h>[01]\d|2[0-3])(?P<m>[0-5]\d)\s*(?:hours|local|zulu)\b", re.I),
    re.compile(r"\b(?P<noon>noon)\b", re.I),
]
UNTIL = re.compile(r"\b(?:until|through|thru|till|ends?|ending|expires?|deadline(?: is)?|no later than)\s+", re.I)
DUE = re.compile(r"\b(?:by|before|no later than|due)\s+", re.I)

_PLACE_WORDS = (r"Library|Church|Hall|Center|Centre|School|Park|EOC|Station|Building|Fairgrounds?|Hospital|Office|"
                r"Room|Club|Restaurant|Cafe|Café|Grange|Armory|College|University|Field|Courthouse|Airport|Mall|"
                r"Market|Shelter|Arena|Stadium|Lodge|Campground|Trailhead|Plaza|Square")
PLACE_RES = [
    re.compile(r"\b\d{1,5}\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?(?:[A-Z][a-z]+\s+){1,3}"
               r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Highway|Hwy|Way|Court|Ct|Place|Pl)\b\.?"),
    re.compile(r"\b(?:mile marker|milepost|exit)\s+\d+\b", re.I),
    re.compile(r"\b(?:intersection|corner) of\s+(?:[A-Z0-9][\w.]*\s?){1,3}\s*and\s+(?:[A-Z0-9][\w.]*\s?){1,3}"),
    re.compile(rf"\b(?:the\s+)?(?:[A-Z][\w'&.-]*\s){{1,4}}(?:{_PLACE_WORDS})\b"),
    re.compile(r"\b(?:Highway|Hwy|Interstate|I-|Route|State Route|SR|US)\s?-?\d+\b"),
]

# --- kinds ---
ANNOUNCE = re.compile(
    r"\b(announce(?:ment|ments)?|reminder|remind (?:everyone|you|all|folks)|don't forget|do not forget|please note|"
    r"heads up|be advised|for your information|FYI|hamfest|swap ?meet|VE (?:session|test(?:ing)?|exam)|"
    r"licen[cs]e (?:exam|test|class)|field day|club meeting|board meeting|dues|newsletter|registration|"
    r"is cancell?ed|has been cancell?ed|(?:is|has been) postponed)\b", re.I)
EVENT = re.compile(
    r"\b(meeting|exercise|drill|hamfest|swap ?meet|session|class|training|field day|breakfast|lunch|dinner|"
    r"picnic|potluck|exam|testing|event|parade|race|marathon|deployment|activation|open house|fox ?hunt|contest|"
    r"simulated emergency test|clean ?up|work party|presentation|workshop|tour)\b", re.I)
ACTION = [
    re.compile(rf"\b(?P<who>I'll|I will|we'll|we will|{CALL}|[A-Z][a-z]+)\s+(?:will|is going to|'ll)\s+(?P<verb>"
               r"send|bring|email|e-mail|forward|look into|find out|contact|call|post|update|set up|organize|"
               r"coordinate|check on|follow up|get|make|print|order|arrange|schedule|confirm|share|put together|"
               r"write|prepare|reach out|drop off|pick up|deliver|fix|repair|test)\b"),
    re.compile(r"\b(?P<who>I'll|I will|we'll|we will|I'm going to|we're going to)\s+(?P<verb>send|bring|email|"
               r"e-mail|forward|look into|find out|contact|post|update|set up|organize|coordinate|check on|"
               r"follow up|get (?:that|this|the|you|it|a)|make sure|print|order|arrange|schedule|confirm|share|"
               r"put together|write|prepare|reach out|drop off|pick up|deliver|fix|repair)\b", re.I),
    re.compile(r"\b(?:volunteers?|help|operators?|drivers?|someone|people) (?:are |is )?(?:needed|wanted)\b", re.I),
    re.compile(r"\b(?:we|they|I) (?:still )?need (?:[\w-]+ ){0,2}(?:volunteers?|operators?|help|"
               r"someone|people|drivers?|radios?|hams)\b", re.I),
    re.compile(r"\blooking for (?:a few |some |more |\d+ )?(?:volunteers?|someone|somebody|operators?|help|people)\b",
               re.I),
    re.compile(r"\bcan (?:someone|anyone|somebody) (?!hear|copy|relay|read)\w+", re.I),
    re.compile(r"\bplease (?:send|bring|e-?mail|contact|let (?:me|us|net control|the \w+) know|sign up|register|"
               r"RSVP|check (?:your|the)|update|submit|forward|call(?! now| in| net control)|reply|respond)\b", re.I),
    re.compile(r"\b(?:action item|to do|follow(?:-| )up on|get back to (?:me|us|you|the net))\b", re.I),
]
DECISION = re.compile(r"\b(?:we )?(?:decided|have decided|agreed|we agree|decision (?:is|was)|motion (?:passed|carried|"
                      r"was approved)|(?:was|were|been) approved|voted (?:to|on)|consensus|it's settled)\b", re.I)
TRAFFIC = re.compile(r"\b(radiogram|formal traffic|(?:I have|with) (?:a |one |two |\d+ )?(?:message|traffic) for|"
                     r"message for|QTC|priority traffic|welfare traffic|health and welfare|ICS[- ]?213|"
                     r"relay(?:ing)? (?:for|a message|traffic|from)|passed the message|message (?:was )?(?:passed|delivered))\b",
                     re.I)
NO_TRAFFIC = re.compile(r"\b(?:no|negative|without|any|nothing|zero)\s+(?:formal |priority |emergency |welfare |net )?"
                        r"(?:traffic|messages?)\b", re.I)

URGENT = {   # category -> cue (whole words, case-insensitive)
    "injury": r"injur(?:y|ies|ed)|(?:is|was|got|badly|been|people) hurt|bleeding|broken (?:leg|arm|bone|hip)|unconscious|not breathing|CPR|ambulance|"
              r"trauma|fell and|has fallen|car accident|crash",
    "fire": r"(?:structure |brush |grass |house |wild|forest |vehicle |car )?fire(?! (?:station|department|dept|"
            r"chief|hall|house|district|truck|company|extinguisher|drill|works|wood))|smoke (?:showing|visible|"
            r"from|coming)|flames|(?:house|home|building|structure|barn|shed|garage|car|vehicle|truck|brush|grass|field|"
            r"trees?|woods) (?:is |are |was )?(?:burning|on fire)|burning (?:house|home|building|structure|barn|car|"
            r"vehicle|brush|grass|field)",
    "flood": r"flood(?:ing|ed|s)?|water (?:is )?rising|high water|washed out|wash ?out|under water|mudslide|"
             r"landslide|debris flow",
    "missing person": r"missing (?:person|child|hiker|hunter|man|woman|boy|girl|kid|resident|\w+ year old)|"
                      r"(?:is|are|been|went|gone|reported) missing|(?:man|woman|child|boy|girl|person|hiker|resident|kid|"
                      r"husband|wife|son|daughter|mother|father) (?:is |was |still )?missing|lost (?:child|hiker|person)|search and rescue|"
                      r"haven't heard from|welfare check|overdue",
    "medical": r"medical|oxygen|insulin|dialysis|heart attack|chest pain|stroke|seizure|diabetic|medication|"
               r"needs? (?:a )?doctor|allergic reaction|overdose|hypothermia|heat stroke|paramedics?|EMS",
    "shelter": r"shelters?|evacuat(?:e|ed|ion|ing)|displaced|red cross|warming center|cooling center",
    "power": r"power (?:is )?(?:out|outage|failure)|no power|without power|lost power|(?:power )?lines? (?:are |is )?down|"
             r"outage|on generator",
    "roads": r"road (?:is )?(?:closed|closure|blocked|out)|roads? closed|closure|blocked|impassable|bridge (?:is )?"
             r"(?:out|closed|down)|tree(?:s)? down|trees? (?:across|on) the road|downed trees?|debris on|"
             r"accident on|rockslide|jackknifed",
    "resources": r"(?:need|needs|needed|requesting|request|short on|out of|running low on|low on)\s+(?:more\s+|some\s+)?"
                 r"(?:water|food|blankets|cots|generators?|fuel|gas|diesel|supplies|sandbags|medical supplies|"
                 r"assistance|manpower|transport|transportation|batteries|radios?)",
    "emergency": r"mayday|life[- ]threatening|emergency traffic|break,? break|not a drill|real world|"
                 r"call(?:ed)? 911|send help",
}
URGENT_RES = {k: re.compile(rf"(?<![A-Za-z0-9])(?:{v})(?![A-Za-z0-9])", re.I) for k, v in URGENT.items()}
NEGATED = re.compile(r"\b(?:no|not|zero|nothing|negative|without any|none)\b[\w\s,]{0,22}$", re.I)   # just before a cue
PREAMBLE = re.compile(r"\b(?:stations? with|anyone with|if (?:you|anyone) ha(?:s|ve)|call now|please call|take|taking|"
                      r"calling for|this net is|for any|first,? then|will be handled|reserved for)\b", re.I)
WEAK = {"medical", "shelter", "roads", "power", "resources"}     # alone, on a net that is not an emergency: needs a place
QUESTION = re.compile(r"^\s*(?:any|is there any|are there any|does anyone have|do we have any|who has)\b", re.I)


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", (text or "").strip())
    return [p.strip() for p in parts if len(p.strip()) > 2]


# ---------- dates and times ----------
def _mon(s: str) -> int:
    s = s.lower().rstrip(".")[:3]
    return [m[:3] for m in MONTHS].index(s) + 1


def _wd(s: str) -> int:
    s = s.lower()[:3]
    return [w[:3] for w in WEEKDAYS].index(s)


def _ahead(d: date, ref: date) -> date:
    """A month/day said without a year: this year, unless that was more than two months ago."""
    return d if d >= ref - timedelta(days=60) else d.replace(year=d.year + 1)


def find_date(s: str, ref: date) -> tuple[date | None, tuple[int, int] | None]:
    """The first date said in `s` (relative to the day it was said), and where it was said."""
    best = None
    for i, rx in enumerate(DATE_RES):
        for m in rx.finditer(s):
            g = m.groupdict()
            try:
                if g.get("mon") and g.get("day"):
                    d = _ahead(date(ref.year, _mon(g["mon"]), int(g["day"])), ref)
                elif g.get("m") and g.get("day"):
                    y = int(g["y"]) if g.get("y") else None
                    y = (2000 + y if y and y < 100 else y) or ref.year
                    d = date(y, int(g["m"]), int(g["day"]))
                    if not g.get("y"):
                        d = _ahead(d, ref)
                elif g.get("wd"):
                    k = _wd(g["wd"])
                    if g.get("day"):                                   # "Saturday the 14th"
                        day = int(g["day"])
                        d = date(ref.year, ref.month, day) if day >= ref.day else \
                            (date(ref.year + (ref.month == 12), ref.month % 12 + 1, day))
                    else:
                        add = (k - ref.weekday()) % 7
                        if add == 0 and (g.get("rel") or "").lower().startswith("next"):
                            add = 7
                        d = ref + timedelta(days=add)
                elif g.get("word"):
                    d = ref + timedelta(days=1 if g["word"].lower().startswith("tomorrow") else 0)
                elif g.get("day"):
                    day = int(g["day"])
                    d = date(ref.year, ref.month, day) if day >= ref.day else \
                        date(ref.year + (ref.month == 12), ref.month % 12 + 1, day)
                else:
                    continue
            except ValueError:
                continue
            if best is None or m.start() < best[1][0]:
                best = (d, m.span())
        if best and i < 3:                     # an explicit calendar date beats a weekday said later
            break
    return (best[0], best[1]) if best else (None, None)


def find_time(s: str) -> str | None:
    for rx in TIME_RES:
        m = rx.search(s)
        if not m:
            continue
        g = m.groupdict()
        if g.get("noon"):
            return "12:00"
        h, mi = int(g["h"]), int(g.get("m") or 0)
        if g.get("ap"):
            h = h % 12 + (12 if g["ap"].lower() == "p" else 0)
        return f"{h:02d}:{mi:02d}"
    return None


def _date_after(rx: re.Pattern, s: str, ref: date) -> date | None:
    for m in rx.finditer(s):
        d, _ = find_date(s[m.end():m.end() + 40], ref)
        if d:
            return d
    return None


def find_place(s: str) -> str | None:
    for rx in PLACE_RES:
        m = rx.search(s)
        if m:
            p = m.group(0).strip(" .,")
            p = re.sub(r"^(?:the|at|in|on)\s+", "", p, flags=re.I)
            if p and not re.fullmatch(r"(?:The|This|That|Net|Net Control)\s+\w+", p):
                return p
    return None


def find_people(s: str, calls: list[str]) -> list[str]:
    out = [c for c in calls if c in s.upper()]
    for m in re.finditer(r"\b(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", s):
        out.append(m.group(0))
    for m in re.finditer(r"\b(?:named|name is|called)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", s):
        out.append(m.group(1))
    for m in re.finditer(r"\b(?:an? )?(?:\d+[- ]year[- ]old|elderly|young|older)\s+(?:man|woman|male|female|boy|girl|"
                         r"child|person|resident|hiker)\b", s, re.I):
        out.append(re.sub(r"^an?\s+", "", m.group(0).strip(), flags=re.I))
    seen = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return seen


# ---------- extraction ----------
def _is_question(s: str) -> bool:
    return s.rstrip().endswith("?") or bool(QUESTION.match(s))


def _norm(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def similar(a: str, b: str, th=0.6) -> bool:
    x, y = _norm(a), _norm(b)
    return bool(x and y) and len(x & y) / len(x | y) >= th


def urgent_categories(s: str) -> list[str]:
    """The urgent categories a sentence reports, leaving out cues said with a negation ("no injuries")."""
    out = []
    for k, rx in URGENT_RES.items():
        for m in rx.finditer(s):
            if not NEGATED.search(s[max(0, m.start() - 30):m.start()]):
                out.append(k)
                break
    return out


def _who(m: re.Match, speaker: str | None) -> str | None:
    if "who" not in m.groupdict():
        return None                                     # a request: nobody has taken it yet
    w = (m.groupdict().get("who") or "").strip()
    if not w or re.fullmatch(r"(?:I'll|I will|I'm going to)", w, re.I):
        return speaker
    if re.fullmatch(r"(?:we'll|we will|we're going to)", w, re.I):
        return "the net"
    if w in ("It", "This", "That", "There", "Everyone", "Somebody", "Someone", "Who", "What", "He", "She", "They"):
        return None
    return w.upper() if re.fullmatch(CALL, w.upper()) else w


def extract(views: list[dict], ref: date | None = None, emergency: bool = False) -> list[dict]:
    """Suggested minutes items from a net's transmissions (each a view: id, start, text, speaker, callsigns,
    hint_keys). `ref`: the day of the net (relative dates are read from each transmission's own day)."""
    out: list[dict] = []
    for v in views:
        if v.get("noise"):
            continue
        text = v.get("text") or ""
        try:
            day = datetime.fromisoformat(v["start"]).date()
        except (KeyError, ValueError):
            day = ref or date.today()
        calls = v.get("callsigns") or []
        spk = v.get("speaker")
        per_kind: dict[str, int] = {}

        def add(kind, s, **kw):
            n = per_kind.get(kind, 0)
            per_kind[kind] = n + 1
            item = {"kind": kind, "text": s.strip(), "by": spk, "tx_id": v.get("id"), "tx_start": v.get("start"),
                    "fp": f"{kind}:{v.get('id')}:{kw.get('category') or ''}:{n}"}
            item.update({k: x for k, x in kw.items() if x not in (None, "", [])})
            out.append(item)

        for s in sentences(text):
            q = _is_question(s)
            am = next((m for rx in ACTION for m in [rx.search(s)] if m), None)
            promise = bool(am and am.groupdict().get("who"))           # "I will contact the EOC": an action item
            if not q and not promise:    # urgent: the strongest signal first; never a question or a denied cue
                cats = urgent_categories(s)
                if cats == ["emergency"] and PREAMBLE.search(s):
                    cats = []                                         # net control's preamble, not a report
                # outside an emergency net a short fragment with one cue is usually a mis-heard word
                # ("He's burning river!"): it needs a second cue or a place, or at least 5 words
                strong = emergency or len(cats) > 1 or find_place(s) or \
                    (set(cats) - WEAK and (len(s.split()) >= 5 or "emergency" in cats))
                if cats and strong:
                    add("urgent", s, category=cats[0], categories=cats, people=find_people(s, calls),
                        place=find_place(s))
                    continue
                if TRAFFIC.search(s) and not NO_TRAFFIC.search(s):
                    add("traffic", s, people=find_people(s, calls))
                    continue
            d, _ = find_date(s, day)
            t = find_time(s)
            until = _date_after(UNTIL, s, day)
            ann = bool(ANNOUNCE.search(s))
            if ann:
                ev = d if d and d != until else None
                add("announcement", s, event_at=_at(ev, t), until=until.isoformat() if until else None,
                    place=find_place(s))
            if q:
                continue
            if DECISION.search(s):
                add("decision", s)
            if am and not re.search(r"\b(?:check(?:ing)? in|stand(?:ing)? by|turn it back|see you|be listening)\b",
                                    s, re.I):
                due = _date_after(DUE, s, day)
                add("action", s, who=_who(am, spk), due=due.isoformat() if due else None)
            elif not ann and (d or t) and EVENT.search(s):
                if d is None:                                         # "the meeting is at 7 PM": that day
                    d = day
                if d > day or (d == day and t):                       # "tonight" alone is the net itself
                    add("event", s, event_at=_at(d, t), place=find_place(s))
    return dedupe(out)


def _at(d: date | None, t: str | None) -> str | None:
    if not d:
        return None
    return f"{d.isoformat()}T{t}" if t else d.isoformat()


def dedupe(items: list[dict]) -> list[dict]:
    """The same announcement read at the start and again at the end of a net is one item (the first)."""
    out = []
    for it in items:
        if any(o["kind"] == it["kind"] and similar(o["text"], it["text"]) for o in out):
            continue
        out.append(it)
    return out


def refine(items: list[dict], views: list[dict], cfg: dict | None = None) -> list[dict]:
    """Optional LLM refinement of the drafted items (cfg: `minutes: {llm: {enabled: true, ...}}` in station.yaml).
    Not wired to any network service yet: the station is offline-first, so this returns the rule-based drafts
    unchanged. A refinement must keep each item's tx_id link and must never touch operator edits."""
    return items


# ---------- validity ----------
def still_valid(it: dict, today: date, net_day: date | None = None, undated_days: int = 28) -> tuple[bool, str]:
    """Is an announcement still worth reading out? (valid, why)."""
    until = it.get("until")
    ev = (it.get("event_at") or "")[:10] or None
    if until:
        return (until >= today.isoformat(), f"until {until}")
    if ev:
        return (ev >= today.isoformat(), f"on {ev}")
    if net_day and (today - net_day).days > undated_days:
        return (False, f"no date given; first heard {net_day.isoformat()}")
    return (True, "no end date given: check it is still current")
