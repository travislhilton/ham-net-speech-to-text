"""Near-miss callsign correction. Whisper on weak radio audio garbles calls by one letter or digit
('WB6BQW' for WB6DQW, 'W7XYZ' for W9XYZ, 'KA7XT' for KA7XP). A garbled token is corrected only when exactly
one real call near it is clearly more likely than anything else, judged by what this station already knows:

  - calls heard earlier in this net (same radio + frequency) and today on this station,
  - the operator's own call and the roster (config/vocab.yaml + local.yaml),
  - the FCC database: licences that are Active, in the station's state or call district, or whose city is
    spoken in the same transmission ("KA7XT, Cypress Creek" -> KA7XP of Cypress Creek).

A wrong correction is worse than none, so a candidate must score >= MIN_SCORE (1.5) and beat every other reading
(the heard token itself included) by >= MARGIN. Otherwise the token is left as heard and listed under
`maybe` with its candidates. Every correction keeps what was heard:
    {"heard": "W7XYZ", "call": "W9XYZ", "why": "your own callsign"}

Speed: candidates for the FCC are generated from the heard token (one confusable substitution, one insertion
or deletion: a few hundred strings) and looked up in one indexed query on the existing hd(call) index, so no
extra index or memory is needed for the 1.7 M licences (~2-5 ms per token). Calls already heard are compared
directly (a weighted edit distance that also allows two confusable substitutions: 'WB08Q' -> WD0AQ).
"""
import re
from collections import Counter

from hamkit.callsigns import _ANY as ANY_CALL

# Letters/digits that sound alike on a noisy channel (spelled-letter rhymes + digit/letter look-alikes).
GROUPS = ["BCDEGPTVZ", "MN", "FSX", "0O", "1LI", "5S", "8AH", "AJK", "IY", "QU"]
CONFUSE: dict[str, set[str]] = {}
for _g in GROUPS:
    for _c in _g:
        CONFUSE.setdefault(_c, set()).update(set(_g) - {_c})
DIGITS = "0123456789"
ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + DIGITS

US_CALL = re.compile(r"^[AKNW][A-Z]?\d[A-Z]{1,3}$")
# A token that may be a garbled call: 0-3 letters, a digit, then letters/digits; 4-7 characters, >= 2 letters.
LOOSE = re.compile(r"^[A-Z]{1,3}\d[A-Z0-9]{1,4}$")
TOKEN = re.compile(r"(?<![A-Za-z0-9/-])([A-Z0-9]{1,8}(?:-[A-Z0-9]{1,6}){0,2})(?![A-Za-z0-9/-])")
# "KD7, XYA" / "KG7 XWK": a prefix with its digit, then the suffix written apart (uppercase only).
SPLIT = re.compile(r"\b([AKNW][A-Z]?\d)[,]?\s+([A-Z]{1,3})\b(?![a-z])")

DISTRICT = {s: d for d, states in {
    "1": "CT ME MA NH RI VT", "2": "NJ NY", "3": "DE DC MD PA", "4": "AL FL GA KY NC SC TN VA",
    "5": "AR LA MS NM OK TX", "6": "CA", "7": "AZ ID MT NV OR UT WA WY", "8": "MI OH WV",
    "9": "IL IN WI", "0": "CO IA KS MN MO NE ND SD"}.items() for s in states.split()}

# Prior belief in a candidate, by where it is known from. Score = prior - COST_WEIGHT * edit cost.
P_NET, P_NET_REPEAT, P_TODAY, P_ROSTER = 3.0, 0.5, 2.0, 2.0
# The operator's own call is in Whisper's prompt (Station.prompt_for), so Whisper also INVENTS it: the 3.915 net
# re-run with large-v3 wrote W9XYZ into 12 of 161 transmissions the operator never made. Own call = roster
# weight, and it never counts as "heard in this net".
P_ME = 2.0
P_JOIN = 1.5            # "KG7, XWK" is joined only when the whole call is known here or its city is spoken
P_CITY, P_CITY_PART, P_STATE, P_DISTRICT = 2.5, 1.5, 1.0, 0.5
P_WEAK = 0.5            # the heard token itself when it is a lapsed licence or a non-US call
# MIN_SCORE 1.5: a call heard in this net may differ by ONE confusable letter (3 - 1), or by two / a digit / an
# insertion only when heard twice (3.5 - 2); an FCC-only call needs its city spoken (2.5 - 1). A single sighting at
# two edits is left as 'maybe': with ~60 calls in a net and 9 letters in the B/C/D/E/G/P/T/V/Z rhyme set, a stranger's
# call is often two rhymes from someone already heard (3.915 net: 'W7BZQ, Shelbyville' sits two rhymes from W7TDQ).
COST_WEIGHT, MIN_SCORE, MARGIN = 2.0, 1.5, 1.0
SUB_CONFUSED, SUB_OTHER, INDEL = 0.5, 1.0, 1.0


def sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if b in CONFUSE.get(a, ()):
        return SUB_CONFUSED
    return SUB_OTHER


def distance(a: str, b: str, cap: float = 1.0) -> float | None:
    """Weighted edit distance (confusable substitution 0.5, other 1, insert/delete 1); None when above cap."""
    if abs(len(a) - len(b)) > cap:
        return None
    prev = [j * INDEL for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        cur = [i * INDEL] + [0.0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + INDEL, cur[j - 1] + INDEL, prev[j - 1] + sub_cost(a[i - 1], b[j - 1]))
        if min(cur) > cap:
            return None
        prev = cur
    return prev[-1] if prev[-1] <= cap else None


def edits(tok: str) -> dict[str, float]:
    """Every US-shaped call one edit from `tok`: a confusable substitution (or any digit for a digit),
    one insertion, one deletion; with the cost of that edit."""
    out: dict[str, float] = {}
    def put(s, c):
        if s != tok and US_CALL.match(s) and c < out.get(s, 9):
            out[s] = c
    for i, ch in enumerate(tok):
        for r in CONFUSE.get(ch, ()):
            put(tok[:i] + r + tok[i + 1:], SUB_CONFUSED)
        if ch.isdigit():
            for r in DIGITS:
                if r != ch:
                    put(tok[:i] + r + tok[i + 1:], SUB_OTHER)
        put(tok[:i] + tok[i + 1:], INDEL)
    for i in range(len(tok) + 1):
        for r in ALNUM:
            put(tok[:i] + r + tok[i:], INDEL)
    return out


def fcc_active(con, calls) -> dict[str, dict]:
    """{call: {"state", "city"}} for the calls with an Active FCC licence (one query per 900 calls)."""
    calls, out = list(calls), {}
    for i in range(0, len(calls), 900):
        chunk = calls[i:i + 900]
        q = ("SELECT hd.call, en.state, en.city FROM hd LEFT JOIN en USING (usi) WHERE hd.status = 'A' "
             f"AND hd.call IN ({','.join('?' * len(chunk))})")
        for call, state, city in con.execute(q, chunk):
            out[call] = {"state": (state or "").upper(), "city": (city or "").upper()}
    return out


def fcc_status(con, calls) -> dict[str, str]:
    """{call: 'A' if any Active licence else the latest status} for calls the FCC knows."""
    calls, out = list(calls), {}
    if not calls:
        return out
    q = f"SELECT call, status FROM hd WHERE call IN ({','.join('?' * len(calls))})"
    for call, st in con.execute(q, calls):
        if out.get(call) != "A":
            out[call] = st
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Z]+", text.upper())


def city_said(city: str, words: set[str], text_up: str) -> float:
    """P_CITY when the licence's city is spoken in the transmission, P_CITY_PART for one long word of a
    multi-word city ('Cypress Greek' for CYPRESS CREEK), else 0."""
    if not city:
        return 0.0
    cw = _words(city)
    if not cw:
        return 0.0
    if re.search(r"\b" + r"\s+".join(map(re.escape, cw)) + r"\b", text_up):
        return P_CITY
    if len(cw) >= 2 and any(len(w) >= 5 and w in words for w in cw):
        return P_CITY_PART
    return 0.0


class Context:
    """What the station knows when it reads one transmission."""
    def __init__(self, me: str = "", roster=(), net=None, today=(), home_state: str | None = None,
                 districts=()):
        self.me = (me or "").upper()
        self.roster = set(roster)
        self.net = Counter({c: n for c, n in Counter(net or ()).items() if c != self.me})  # this net, this tx excluded
        self.today = set(today) - set(self.net) - {self.me}  # heard today on this station, outside this net
        self.home_state = (home_state or "").upper() or None
        self.districts = set(districts)

    def known(self) -> dict[str, tuple[float, str]]:
        """Calls known without the FCC: {call: (prior, why)}; the strongest reason wins."""
        k: dict[str, tuple[float, str]] = {}
        def put(c, p, why):
            if c and p > k.get(c, (-1, ""))[0]:
                k[c] = (p, why)
        put(self.me, P_ME, "your own callsign")
        for c in self.roster:
            put(c, P_ROSTER, "on the roster")
        for c in self.today:
            put(c, P_TODAY, "heard earlier today")
        for c, n in self.net.items():
            put(c, P_NET + (P_NET_REPEAT if n >= 2 else 0), "heard in this net" if n == 1
                else f"heard {n} times in this net")
        return k


class CallFixer:
    """`con` is the FCC sqlite connection (None = no FCC: context only); `lock` guards it; `validate` is the
    callbook's validate (roster / FCC any status / country file)."""
    def __init__(self, con=None, lock=None, validate=None):
        self.con, self.lock, self.validate = con, lock, validate

    def _q(self, fn, calls):
        if not self.con:
            return {}
        if self.lock:
            with self.lock:
                return fn(self.con, calls)
        return fn(self.con, calls)

    def home(self, call: str) -> str | None:
        """The FCC state of a callsign (the operator's, for the station's region)."""
        r = self._q(fcc_active, [call]) if call else {}
        return (r.get(call) or {}).get("state") or None

    # ---- one token ----
    def candidates(self, tok: str, ctx: Context, text: str, fcc_ok=True) -> list[dict]:
        known = ctx.known()
        cands: dict[str, dict] = {}
        for c, (p, why) in known.items():
            d = distance(tok, c, cap=1.0)
            if d is not None and c != tok:
                cands[c] = {"call": c, "cost": d, "prior": p, "why": why}
        if fcc_ok and self.con:
            near = edits(tok)
            info = self._q(fcc_active, list(near))
            up = text.upper(); words = set(_words(text))
            for c, inf in info.items():
                p, why = 0.0, "FCC licence"
                cs = city_said(inf["city"], words, up)
                if cs:
                    p, why = cs, f"FCC licence in {inf['city'].title()}, named in this transmission"
                elif ctx.home_state and inf["state"] == ctx.home_state:
                    p, why = P_STATE, f"FCC licence in {inf['state']}"
                elif DISTRICT.get(inf["state"]) in ctx.districts:
                    p, why = P_DISTRICT, f"FCC licence in {inf['state']}"
                if c in cands:                       # known AND near: keep the stronger reason, add the city
                    if cs:
                        cands[c]["prior"] += cs
                        cands[c]["why"] += f"; {inf['city'].title()} named in this transmission"
                    continue
                cands[c] = {"call": c, "cost": near[c], "prior": p, "why": why}
        for c in cands.values():
            c["score"] = round(c["prior"] - COST_WEIGHT * c["cost"], 2)
        return sorted(cands.values(), key=lambda c: -c["score"])

    def decide(self, tok: str, own: float, ctx: Context, text: str, fcc_ok=True):
        """-> ("fix", cand) | ("maybe", [cands]) | (None, None). `own` = the heard token's own score."""
        cs = self.candidates(tok, ctx, text, fcc_ok)
        if not cs:
            return None, None
        best = cs[0]
        second = max([own] + [c["score"] for c in cs[1:2]])
        if best["score"] >= MIN_SCORE and best["score"] - second >= MARGIN:
            return "fix", best
        strong = [c for c in cs if c["score"] >= 0][:3]
        return ("maybe", strong) if strong else (None, None)

    # ---- one transmission ----
    def fix(self, text: str, calls: list[str], ctx: Context):
        """-> (text, calls, corrections, maybe). `calls` is normalize()'s list (validated calls, in order)."""
        corrections, maybe = [], []
        calls = list(calls)
        known = ctx.known()
        fs = self._q(fcc_status, set(calls)) if self.con else {}

        def strength(c: str) -> float | None:
            """None = solid (leave it); else the heard call's own score as a reading."""
            if c in known:
                return None
            if self.con and US_CALL.match(c):
                if c not in fs:
                    fs[c] = self._q(fcc_status, [c]).get(c)
                st = fs[c]
                return None if st == "A" else (P_WEAK if st else 0.0)
            if self.con:                              # non-US call the country file accepted
                return P_WEAK
            return None                               # no FCC: trust validate()

        # 1. calls normalize() accepted but that are weak (lapsed licence, a DX prefix, a phonetic run the FCC
        #    does not know): a nearby call heard here may be what was said
        for i, c in enumerate(calls):
            own = strength(c)
            if own is None:
                continue
            kind, r = self.decide(c, own, ctx, text, fcc_ok=US_CALL.match(c) is not None)
            if kind == "fix" and r["call"] not in calls:
                text = re.sub(r"\b" + re.escape(c) + r"\b", r["call"], text)
                calls[i] = r["call"]
                corrections.append({"heard": c, "call": r["call"], "why": r["why"]})
            elif kind == "maybe":
                maybe.append({"heard": c, "candidates": [x["call"] for x in r]})

        # 2. "KD7, XYA" written apart: joined when the whole call is clearly real here
        def join(m):
            heard, tok = m.group(0), m.group(1) + m.group(2)
            if tok in calls:
                return heard
            ok = tok in known or (self.con and self._q(fcc_active, [tok]))
            if not ok:
                return heard
            own_c = {"call": tok, "cost": 0.0}
            if tok in known:
                own_c.update(prior=known[tok][0], why=known[tok][1])
            else:
                inf = self._q(fcc_active, [tok])[tok]
                p = city_said(inf["city"], set(_words(text)), text.upper())
                own_c.update(prior=p or (P_STATE if inf["state"] == ctx.home_state else 0.0),
                             why=f"FCC licence in {inf['city'].title() if p else inf['state']}")
            others = self.candidates(tok, ctx, text)
            second = max([c["score"] for c in others[:1]] + [0.0])
            if own_c["prior"] >= P_JOIN and own_c["prior"] - second >= MARGIN:
                calls.append(tok)
                corrections.append({"heard": heard, "call": tok, "why": "written apart; " + own_c["why"]})
                return tok
            return heard
        text = SPLIT.sub(join, text)

        # 3. call-shaped tokens nothing accepted ('WB6BQW', 'KD-0XHR')
        def tokfix(m):
            heard = m.group(1)
            tok = heard.replace("-", "")
            if tok in calls or not LOOSE.match(tok) or sum(ch.isalpha() for ch in tok) < 2:
                return heard
            if tok[0] not in "AKNW" and not ANY_CALL.match(tok):     # 'ICS-213', 'MX759': not a garbled call
                return heard
            if self.validate and self.validate(tok) and strength(tok) is None:
                if heard != tok and ANY_CALL.match(tok):            # a real call written with a hyphen
                    calls.append(tok)
                    return tok
                return heard
            kind, r = self.decide(tok, 0.0, ctx, text, fcc_ok=True)
            if kind == "fix" and r["call"] not in calls:
                calls.append(r["call"])
                corrections.append({"heard": heard, "call": r["call"], "why": r["why"]})
                return r["call"]
            if kind == "maybe":
                maybe.append({"heard": heard, "candidates": [x["call"] for x in r]})
            return heard
        text = TOKEN.sub(tokfix, text)
        return text, calls, corrections, maybe
