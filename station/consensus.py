"""Net-wide callsign agreement: one station, one call, across a whole net.

Whisper hears the same station several ways in one net (KD7ABP / KD7ABD, a clipped "87QF"), so a 140-transmission
net shows 57 "stations", 51 heard once. Per-transmission correction (hamkit/callfix.py) sees one transmission at
a time; this module looks at the whole net (or the net so far) and asks, for every call heard: which real
station was this most likely?

Candidates for a heard call h:
  - h itself;
  - the other calls heard in this net within a weighted edit distance of 1.5 (letters Whisper confuses cost 0.5:
    B/C/D/E/G/P/T/V/Z, M/N, F/S/X, 1/7, ...; any other letter, an insertion or a deletion cost 1);
  - FCC licences one edit (or two confusable letters) from h that are Active — these were never heard, so they
    need evidence of their own (a name, a town or a prior check-in), never only "it exists";
  - for a clipped call ("87QF", "he's 7QX": up to 2 leading characters missing): completed only when exactly ONE
    Active licence in the station's state ends in that digit + suffix, and its licensee's first name is said with
    the call or in net control's direct reply, or it checked in to this net before; two or more fit = left as
    heard with the candidates (owner, 2026-11-03).

Evidence, weighed per candidate (W_* below):
  big     Active FCC licence; the licensee's first name (or its nickname: Bob/Robert, Wally/Wallace) said right
          after the call in its own transmission, or in the reply that follows it directly ("Thank you, Joseph";
          an empty transmission or the call said again is skipped), never past the next OTHER call;
  medium  a town said there that is the licence's city; licensed in the station's state; took part in this
          same net before (history.db); on the roster;
  small   how often it was heard in this net;
  cost    the edit distance from what was heard.

The best candidate replaces h only when it beats what was heard AND the runner-up by MARGIN, and passes these
rules whatever its score (owner review of the first three live nets, 2026-11-03):
  - a call never heard needs a name, town or history behind it; so does moving a heard call that is itself an
    Active licence ("licensed in the state" alone never moves a live licence);
  - more than 1 plain letter change needs the name said WITH the call, or a name and earlier check-ins;
  - never onto a call heard in the same transmission; two calls each heard in their own check-in turn (never in
    adjacent transmissions) are two stations unless they differ by 1 letter and one is no Active licence;
  - a name moves one call only: the call it was said with, else the best-scored; the others are judged again
    without it.
Otherwise h stays as heard: "verified" when it has its own support, "unverified" when nothing supports it (not in
the FCC, no name, no history) — flagged, never invented away.

QRZ.com (owner, 11-04): a call the FCC does not show Active is looked up there (the station does it; `qrz` here is
the answers). A current QRZ record confirms it; when QRZ answers with ANOTHER call that lists the heard one as an
alias, the heard call is that operator's old call and moves to the current one (on VHF/UHF only within reach). Every
call ends up "confirmed" (an Active FCC licence, a current QRZ record, the roster) or not, with the reason; an
unconfirmed call is still counted.

Pure: no network, no globals. The FCC is any object with info()/neighbours()/tails() (Licences below wraps the
station's sqlite copy); `prior` is {call: earlier nets checked in}. Same input -> same output (idempotent)."""
import hashlib
import math
import re
from collections import Counter

from hamkit import callfix
from hamkit.callsigns import _ANY as ANY_CALL

US_CALL = callfix.US_CALL
W_ACTIVE, W_LAPSED = 3.0, 0.5
W_NAME = 3.0
W_TOWN, W_TOWN_PART, W_STATE = 2.0, 1.0, 1.0
W_STATE_LOCAL = 2.0                            # on VHF/UHF (a repeater or simplex net) a licence in-state counts more
W_HISTORY, W_HISTORY_MANY = 2.0, 0.5          # took part in this net before (>= 3 times: a little more)
W_ROSTER = 1.5
W_FREQ, W_FREQ_MAX = 0.3, 1.2                  # each extra hearing in this net
W_COST = 1.0                                   # per unit of edit distance from what was heard
TAIL_COST = 0.5                                # a clipped call: the prefix was not heard
CLUSTER_CAP = 1.5
MARGIN = 1.5
MIN_SCORE = 3.0
MAX_SUBS = 1            # letters changed on FCC + a name in the reply alone
LOCAL_KM = 150          # VHF/UHF: a licence this close is in range of the net (the 11-03 nets: every station heard
                        # 3+ times was within 50 km; 32 of 33 licences beyond 250 km were heard once = misreadings)
REACH_KM = 300         # ... and beyond this a linked system rarely reaches: the 11-03 nets' clean roll-call check-ins
                       # ran to 294 km; past 400 km only 5 of 59 licences were read alike by both passes (misreadings)
W_ALIAS = 3.0           # QRZ.com: the heard call is an earlier call of this operator
NEAR_KM = 50            # ... and this close, the clearly nearest licence completes a clipped call on its own
MAX_PREFIX = 2          # leading characters a clipped call may be missing ("7QF" -> AD7QF)


def levenshtein(a: str, b: str) -> int:
    """Plain edit count: every changed, added or dropped character is 1, however alike it sounds."""
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[-1]

EXTRA_CONFUSE = [set("17"), set("MN"), set("QG"), set("0O")]

NICK = {
    "ROBERT": "BOB BOBBY ROB ROBBIE BERT", "WILLIAM": "BILL BILLY WILL WILLIE LIAM", "RICHARD": "RICK RICKY DICK RICH",
    "RICKEY": "RICK RICKY", "RICKY": "RICK", "JAMES": "JIM JIMMY JAMIE", "JOHN": "JACK JOHNNY", "JOSEPH": "JOE JOEY",
    "THOMAS": "TOM TOMMY", "CHARLES": "CHUCK CHARLIE CHAS", "EDWARD": "ED EDDIE TED NED", "THEODORE": "TED TEDDY",
    "EDWIN": "ED EDDIE", "WALLACE": "WALLY", "WALTER": "WALT WALLY", "LAWRENCE": "LARRY", "LAURENCE": "LARRY",
    "DAVID": "DAVE", "DANIEL": "DAN DANNY", "MICHAEL": "MIKE MICK", "STEVEN": "STEVE", "STEPHEN": "STEVE",
    "KENNETH": "KEN KENNY", "GREGORY": "GREG", "GERALD": "JERRY", "JEROME": "JERRY", "DONALD": "DON DONNIE",
    "RONALD": "RON RONNIE", "ANTHONY": "TONY", "NICHOLAS": "NICK", "TIMOTHY": "TIM", "DOUGLAS": "DOUG",
    "JEFFREY": "JEFF", "JEFFERY": "JEFF", "GEOFFREY": "JEFF GEOFF", "MATTHEW": "MATT", "ANDREW": "ANDY DREW",
    "BENJAMIN": "BEN", "SAMUEL": "SAM", "PATRICK": "PAT", "PATRICIA": "PAT PATTY TRISH", "ALBERT": "AL BERT",
    "ALAN": "AL", "ALLEN": "AL", "ALLAN": "AL", "ALFRED": "AL ALF", "RAYMOND": "RAY", "EUGENE": "GENE",
    "FREDERICK": "FRED FREDDY", "HAROLD": "HAL HARRY", "HENRY": "HANK HARRY", "PHILIP": "PHIL", "PHILLIP": "PHIL",
    "LEONARD": "LEN LENNY", "VINCENT": "VINCE", "RANDALL": "RANDY", "RANDOLPH": "RANDY", "TERRENCE": "TERRY",
    "TERRELL": "TERRY", "TERENCE": "TERRY", "CHRISTOPHER": "CHRIS", "CHRISTINE": "CHRIS", "KATHERINE": "KATE KATHY",
    "KATHLEEN": "KATHY", "ELIZABETH": "LIZ BETH BETTY", "MARGARET": "MAGGIE PEGGY", "SUSAN": "SUE", "DEBORAH": "DEB DEBBIE",
    "DEBRA": "DEB DEBBIE", "CYNTHIA": "CINDY", "JENNIFER": "JEN JENNY", "BARBARA": "BARB", "VALERIE": "VAL",
    "DOROTHY": "DOT DOTTIE", "BRADLEY": "BRAD", "BRADFORD": "BRAD", "NATHAN": "NATE", "NATHANIEL": "NATE",
    "ZACHARY": "ZACK ZACH", "JONATHAN": "JON", "JOSHUA": "JOSH", "CRAIG": "", "LAURIE": "LAURA", "LAURA": "LAURIE",
    "ALEXANDER": "ALEX", "MITCHELL": "MITCH", "FRANKLIN": "FRANK", "FRANCIS": "FRANK", "MARVIN": "MARV",
    "DWIGHT": "", "ERNEST": "ERNIE", "HOWARD": "HOWIE", "LEROY": "ROY", "ELDON": "", "DUANE": "DEWAYNE",
}


def first_names(first: str | None) -> set[str]:
    """The licensee's first name and its nicknames, upper case ('ROBERT' -> ROBERT, BOB, ROB, ...)."""
    f = re.sub(r"[^A-Z ]", "", (first or "").upper()).split()
    if not f or len(f[0]) < 2:
        return set()
    out = {f[0]} | set(NICK.get(f[0], "").split())
    out |= {full for full, nicks in NICK.items() if f[0] in nicks.split()}      # the licence says BOB
    return out


def _sub(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if callfix.sub_cost(a, b) < 1 or any(a in g and b in g for g in EXTRA_CONFUSE):
        return 0.5
    return 1.0


def distance(a: str, b: str, cap: float = CLUSTER_CAP) -> float | None:
    """Weighted edit distance (confusable letters 0.5, others / insert / delete 1); None when above cap."""
    if abs(len(a) - len(b)) > cap:
        return None
    prev = [float(j) for j in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        cur = [float(i)] + [0.0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + _sub(a[i - 1], b[j - 1]))
        if min(cur) > cap:
            return None
        prev = cur
    return prev[-1] if prev[-1] <= cap else None


def neighbours(call: str) -> dict[str, float]:
    """US-shaped strings one edit from `call` (callfix.edits) plus two confusable letters (cost 1.0)."""
    out = dict(callfix.edits(call))
    conf = lambda ch: callfix.CONFUSE.get(ch, set()) | {x for g in EXTRA_CONFUSE if ch in g for x in g} - {ch}
    for i, a in enumerate(call):
        for ra in conf(a):
            s1 = call[:i] + ra + call[i + 1:]
            for j in range(i + 1, len(call)):
                for rb in conf(call[j]):
                    s2 = s1[:j] + rb + s1[j + 1:]
                    if US_CALL.match(s2) and s2 != call and out.get(s2, 9) > 1.0:
                        out[s2] = 1.0
            if US_CALL.match(s1) and s1 != call and out.get(s1, 9) > 0.5:
                out[s1] = 0.5
    return out


# ---------- the FCC, as the agreement sees it ----------
class Licences:
    """The station's FCC copy (hamkit/fcc.py sqlite). Every answer is cached for the life of the object."""
    def __init__(self, con, lock=None, geo=None):
        self.con, self.lock, self.geo = con, lock, geo      # geo: hamkit.zipgeo.ZipGeo (the licence's ZIP centre)
        self._info: dict[str, dict | None] = {}
        self._tails: dict[tuple, list[str]] = {}

    def _run(self, sql, args):
        if self.lock:
            with self.lock:
                return self.con.execute(sql, args).fetchall()
        return self.con.execute(sql, args).fetchall()

    def info(self, calls) -> dict[str, dict]:
        """{call: {"status": "A"|other, "first", "city", "state"}} for calls the FCC knows (Active preferred)."""
        want = [c for c in dict.fromkeys(calls) if c not in self._info]
        for i in range(0, len(want), 900):
            chunk = want[i:i + 900]
            for c in chunk:
                self._info[c] = None
            rows = self._run("SELECT hd.call, hd.status, en.first, en.city, en.state, hd.grant_date, en.zip FROM hd"
                             f" LEFT JOIN en USING (usi) WHERE hd.call IN ({','.join('?' * len(chunk))})", chunk)
            for call, st, first, city, state, granted, zipcode in rows:
                cur = self._info.get(call)
                key = (st == "A", granted or "")
                if cur is None or key > cur["_k"]:
                    self._info[call] = {"status": st, "first": first, "city": (city or "").upper(),
                                        "state": (state or "").upper(), "zip": (zipcode or "")[:5], "_k": key}
        return {c: self._info[c] for c in calls if self._info.get(c)}

    def tails(self, tails, state: str | None) -> dict[str, list[str]]:
        """{"7QF": [Active calls in `state` ending in 7QF]} (one table scan per new batch of tails)."""
        want = [t for t in dict.fromkeys(tails) if (t, state) not in self._tails]
        if want and state:
            for t in want:
                self._tails[(t, state)] = []
            cond = " OR ".join("hd.call LIKE ?" for _ in want)
            rows = self._run("SELECT DISTINCT hd.call FROM hd JOIN en USING (usi) WHERE hd.status='A' AND en.state=?"
                             f" AND ({cond})", [state] + ["%" + t for t in want])
            for (call,) in rows:
                for t in want:
                    if call.endswith(t) and US_CALL.match(call):
                        self._tails[(t, state)].append(call)
        return {t: sorted(self._tails.get((t, state), [])) for t in tails}


    def km(self, call: str, where) -> float | None:
        """How far the licence's mailing ZIP centre is from `where` (lat, lon); None when unknown."""
        inf = self._info.get(call) if call in self._info else (self.info([call]).get(call))
        p = self.geo.where((inf or {}).get("zip")) if (self.geo and where) else None
        if p is None:
            return None
        from hamkit.zipgeo import km
        return km(where, p)

    def tails_near(self, tails, where, radius_km: float) -> dict[str, list[str]]:
        """{"7QF": [Active calls ending in 7QF whose ZIP is within radius_km of `where`, any state]} — on VHF/UHF the
        net's area, which may cross a state line."""
        want = [t for t in dict.fromkeys(tails) if (t, "near", radius_km) not in self._tails]
        if want and self.geo and where:
            for t in want:
                self._tails[(t, "near", radius_km)] = []
            cond = " OR ".join("hd.call LIKE ?" for _ in want)
            rows = self._run("SELECT DISTINCT hd.call FROM hd WHERE hd.status='A' AND (" + cond + ")",
                             ["%" + t for t in want])
            calls = [c for (c,) in rows if US_CALL.match(c)]
            self.info(calls)
            for c in calls:
                d = self.km(c, where)
                if d is None or d > radius_km:
                    continue
                for t in want:
                    if c.endswith(t):
                        self._tails[(t, "near", radius_km)].append(c)
        return {t: sorted(self._tails.get((t, "near", radius_km), [])) for t in tails}


def where_of(cfg: dict, grid: str | None, lic, me: str) -> tuple[float, float] | None:
    """The station's (lat, lon): `station_lat`/`station_lon` in station.yaml, else the operator's 6-character grid
    (Settings), else the operator's licence ZIP centre, else a 4-character grid (1 x 2 degrees: coarse)."""
    from hamkit.zipgeo import grid_to_latlon
    if cfg.get("station_lat") is not None and cfg.get("station_lon") is not None:
        return (float(cfg["station_lat"]), float(cfg["station_lon"]))
    grid = str(grid or "").strip()
    if len(grid) == 6 and grid_to_latlon(grid):
        return grid_to_latlon(grid)
    call = (me or "").upper().split("-")[0]
    if lic is not None and getattr(lic, "geo", None) and call:
        p = lic.geo.where((lic.info([call]).get(call) or {}).get("zip"))
        if p:
            return p
    return grid_to_latlon(grid) if grid else None


# ---------- what was said ----------
_TAIL = re.compile(r"(?<![A-Za-z0-9-])\d?(\d[A-Z]{2,3})(?![A-Za-z0-9])")
_CAP = re.compile(r"\b[A-Z][a-z]{1,15}\b")
_NOT_TAIL = {"AM", "PM", "MHZ", "KHZ", "WPM", "DB"}


def _mentions(text: str, calls: list[str]) -> list[tuple[int, str, str]]:
    """[(pos, call-or-tail, kind)]: calls normalize() accepted ("call"), call-shaped tokens it did not ("token",
    'W7KTQ', 'KM7 TQV'), and clipped calls ("tail", '87QF' -> '7QF'). In text order."""
    out, taken = [], []
    up = text or ""
    for c in calls:
        for m in re.finditer(r"(?<![A-Z0-9])" + re.escape(c) + r"(?![A-Z0-9])", up):
            out.append((m.start(), c, "call")); taken.append(m.span())
        if not any(x[1] == c for x in out):
            out.append((len(up), c, "call"))                 # accepted from a spelled-out form
    free = lambda a, b: not any(a < e and s < b for s, e in taken)
    for m in callfix.SPLIT.finditer(up):
        tok = m.group(1) + m.group(2)
        if free(*m.span()) and US_CALL.match(tok) and tok not in calls:
            out.append((m.start(), tok, "token")); taken.append(m.span())
    for m in callfix.TOKEN.finditer(up):
        tok = m.group(1).replace("-", "")
        if (free(*m.span(1)) and tok not in calls and tok[0] in "AKNW" and callfix.LOOSE.match(tok)
                and sum(ch.isalpha() for ch in tok) >= 2 and sum(ch.isdigit() for ch in tok) <= 2):
            out.append((m.start(1), tok, "token")); taken.append(m.span(1))
    for m in _TAIL.finditer(up):
        if free(*m.span()) and m.group(1)[1:] not in _NOT_TAIL:
            out.append((m.start(), m.group(1), "tail")); taken.append(m.span())
    return sorted(out)


def _source_text(v: dict) -> str:
    return v.get("text") or ""


def src_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


# ---------- the agreement ----------
def agree(views: list[dict], fcc=None, prior: dict | None = None, home_state: str | None = None,
          roster=(), me: str = "", live_calls: dict | None = None, local: bool | None = None,
          where: tuple[float, float] | None = None, qrz: dict | None = None,
          reach_km: float | None = None) -> dict:
    """views: one net's transmissions in time order ({"id", "text", "callsigns"}; the accurate text where there is
    one). live_calls: {tx id: the live text's callsigns} — other readings of the same words, used as evidence.
    local: a VHF/UHF net (default: from the views' freq_mhz), where a licence in the station's state counts more.
    where: the station's (lat, lon). On a VHF/UHF net with the ZIP centres installed, distance replaces the state:
    a licence within LOCAL_KM is in range, a clipped call goes to the clearly nearest licence (owner, 11-04).
    A licence beyond REACH_KM that nothing backs (one transmission, the live and accurate passes did not read it
    alike, no name, town, history or roster) is "distant": its text is kept, but it is left out of the calls and so
    of every count. Said once alone is no evidence: a roll call hears most stations once (owner, 11-04).
    qrz: {call: QRZ record, or None = "QRZ.com does not know it"}; a call missing from it was not looked up.
    reach_km: the "distant" limit (Settings; default REACH_KM).
    -> {"calls": {heard: {"call", "conf", "why", "status", "confirmed", "unconfirmed"?, "candidates"?, "km"?}},
        "tx": {id: [calls after agreement]}}"""
    reach = float(reach_km) if reach_km else REACH_KM
    if local is None:
        f = [v.get("freq_mhz") for v in views if v.get("freq_mhz")]
        local = bool(f) and sum(1 for x in f if x >= 30) * 2 > len(f)
    w_state = W_STATE_LOCAL if local else W_STATE
    geo_on = bool(local and where and fcc is not None and getattr(fcc, "geo", None))
    prior = {k.upper(): v for k, v in (prior or {}).items()}
    roster = {c.upper() for c in roster} - {""}
    me = (me or "").upper()
    home_state = (home_state or "").upper() or None
    live_calls = live_calls or {}

    events: list[list[tuple]] = []           # per transmission: [(pos, kind, value)], kind call|token|tail|live
    heard = Counter()                        # full calls -> times heard (accurate + live readings)
    for v in views:
        text = _source_text(v)
        ev = [(p, k, c) for p, c, k in _mentions(text, list(v.get("callsigns") or []))]
        for c in live_calls.get(v.get("id"), []) or []:
            if c not in [e[2] for e in ev]:
                ev.append((-1, "live", c))
        ev.sort(key=lambda e: e[0])
        events.append(ev)
        for _, k, c in ev:
            if k in ("call", "token", "live"):
                heard[c] += 1
    full = [c for c in heard if US_CALL.match(c) or ANY_CALL.match(c)]
    in_tx = Counter(c for ev in events for c in {c for _, k, c in ev if k in ("call", "token", "live")})
    alike = {c for v in views for c in (v.get("callsigns") or []) if c in (live_calls.get(v.get("id")) or [])}
    texts = [_source_text(v) for v in views]
    noise = [bool(v.get("noise")) for v in views]

    # what the FCC says about every call heard, its neighbours, and the clipped calls
    near = {c: neighbours(c) for c in full if US_CALL.match(c)}
    lic = {}
    if fcc is not None:
        lic = fcc.info(set(full) | {n for d in near.values() for n in d} | roster | set(prior))
    tails = sorted({c for ev in events for _, k, c in ev if k == "tail"})
    tail_fcc = ({} if fcc is None or not tails else
                fcc.tails_near(tails, where, 2 * LOCAL_KM) if geo_on else fcc.tails(tails, home_state))
    if tail_fcc:
        lic.update(fcc.info({c for cs in tail_fcc.values() for c in cs} - set(lic)))

    # QRZ.com: confirms a call the FCC does not know, or names the current call of an old one
    qrz = {k.upper(): v for k, v in (qrz or {}).items()}
    today = max((str(v.get("start") or "")[:10] for v in views), default="") or None
    qrz_now: dict[str, str] = {}                 # heard call -> the operator's current call (QRZ alias)
    for c in full:
        rec = qrz.get(c)
        if not rec or (lic.get(c) or {}).get("status") == "A":
            continue
        cur = (rec.get("call") or "").upper()
        if cur == c:
            if not lic.get(c) and qrz_current(rec, today):
                lic[c] = qrz_licence(rec)          # a licence the FCC copy does not have (new, or not a US call)
        elif cur and c in {a.strip().upper() for a in (rec.get("aliases") or "").split(",")}:
            qrz_now[c] = cur
    if qrz_now and fcc is not None:
        lic.update(fcc.info(set(qrz_now.values()) - set(lic)))
    for c, cur in qrz_now.items():
        if cur not in lic:
            lic[cur] = qrz_licence(qrz[c])

    def dist(c):
        if not geo_on:
            return None
        inf = lic.get(c) or {}
        if inf.get("src") == "QRZ.com":
            return round(_km(where, (inf["lat"], inf["lon"]))) if inf.get("lat") is not None else None
        return fcc.km(c, where)

    # where each call was heard in the transcript itself (not the live reading): transmission indexes
    at_tx: dict[str, set] = {}
    for i, ev in enumerate(events):
        for _, k, c in ev:
            if k in ("call", "token"):
                at_tx.setdefault(c, set()).add(i)

    def _repeat_of(c2: str, call: str) -> bool:
        return (c2 == call or (len(c2) >= 3 and call.startswith(c2))       # "N7Y..." repeating N7YPX
                or (call[:1].isdigit() and c2.endswith(call)))             # "87QF" repeating the clipped 7QF

    def windows(call: str, banned: set):
        """(names said in the call's own transmission right after it, names said in the reply that follows it
        directly, text said) — each part stops at the next OTHER call, so a name belongs to the call it follows.
        The reply is the next transmission with words in it that are not just this call again."""
        same, nxt, said = set(), set(), []
        for i, ev in enumerate(events):
            for p, k, c in ev:
                if c != call or k == "live":
                    continue
                t = texts[i]
                b = next((q for q, k2, c2 in ev if k2 != "live" and q > p and not _repeat_of(c2, call)), len(t))
                s1 = t[p:b]
                said.append(s1)
                same |= {w.upper() for w in _CAP.findall(s1)} - banned
                if b < len(t):
                    continue                                   # another call follows in the same transmission
                for j in range(i + 1, len(events)):
                    tj = texts[j]
                    others = [q for q, k2, c2 in events[j] if k2 != "live" and not _repeat_of(c2, call)]
                    left = re.sub(r"[A-Z0-9][A-Z0-9-]{1,9}",
                                  lambda m: "" if _repeat_of(m.group(0).replace("-", ""), call) else m.group(0), tj)
                    if noise[j] or not re.search(r"[A-Za-z]{2}", left):
                        continue                               # empty, noise, or the call said again
                    s2 = tj[:min(others)] if others else tj
                    said.append(s2)
                    nxt |= {w.upper() for w in _CAP.findall(s2)} - banned
                    break
        return same, nxt, " ".join(said)

    def evidence(c: str, same: set, nxt: set, said: str, cost: float, h: str = ""):
        score, why, ev = -W_COST * cost, [], {"name": False, "name_same": False, "hist": False, "strong": False,
                                               "alias": False}
        inf = lic.get(c)
        if h and qrz_now.get(h) == c:
            score += W_ALIAS; ev.update(alias=True, strong=True); why.append(f"QRZ.com: {h} is an earlier call of {c}")
        if inf and inf["status"] == "A":
            score += W_ACTIVE; why.append(f"{inf.get('src') or 'FCC'}: active")
        elif inf:
            score += W_LAPSED; why.append(f"FCC: licence {_STATUS.get(inf['status'], inf['status'])}")
        elif US_CALL.match(c):
            why.append("not in the FCC database")
        if inf:
            fn = first_names(inf.get("first"))
            nm = fn & (same | nxt)
            if nm:
                score += W_NAME; ev.update(name=sorted(nm)[0], name_same=bool(fn & same), strong=True)
                why.append(f"name {sorted(nm)[0].title()} matches"
                           + (" (said with the call)" if fn & same else " (in the reply)"))
            up = said.upper()
            town = callfix.city_said(inf.get("city"), set(re.findall(r"[A-Z]+", up)), up)
            if town >= callfix.P_CITY:
                score += W_TOWN; ev["strong"] = True; why.append(f"town {inf['city'].title()} said")
            elif town:
                score += W_TOWN_PART; ev["strong"] = True; why.append(f"town {inf['city'].title()} partly said")
            elif geo_on and inf["status"] == "A" and dist(c) is not None:
                d = dist(c)
                if d <= LOCAL_KM:
                    score += w_state; why.append(f"{d:.0f} km away")
                else:
                    why.append(f"{d:.0f} km away (beyond VHF/UHF range)")
            elif home_state and inf.get("state") == home_state and inf["status"] == "A":
                score += w_state; why.append(f"licensed in {home_state}")
        n = prior.get(c, 0)
        if n:
            score += W_HISTORY + (W_HISTORY_MANY if n >= 3 else 0); ev.update(hist=True, strong=True)
            why.append(f"{n} prior check-in{'s' if n != 1 else ''} to this net")
        if c in roster and c != me:
            score += W_ROSTER; ev["strong"] = True; why.append("on the roster")
        if heard.get(c, 0) > 1:
            score += min(W_FREQ_MAX, W_FREQ * (heard[c] - 1)); why.append(f"heard {heard[c]} times")
        if cost:
            why.append(f"{cost:g} edit{'s' if cost != 1 else ''} from what was heard")
        return round(score, 2), why, ev

    def active(c):
        return (lic.get(c) or {}).get("status") == "A"

    def allowed(h: str, c: str, is_tail: bool, ev: dict) -> bool:
        """The rules a move must pass whatever its score (owner review of the first live nights, 11-03)."""
        if c == h:
            return True
        if ev.get("alias"):                       # the same operator under the call they hold now
            if geo_on:
                d = dist(c)
                if d is None or d > reach:
                    return False                  # VHF/UHF: out of reach, or nowhere known (a PO box ZIP)
            elif local and home_state and (lic.get(c) or {}).get("state") != home_state:
                return False                      # VHF/UHF without the ZIP centres: the station's state only
            return not (h in at_tx and c in at_tx and at_tx[h] & at_tx[c])
        if not ev["strong"] and (c not in heard or (not is_tail and distance(h, c) > 1.0)):
            return False          # never invent: an unheard licence, or a far one, needs a name, town or history
        if not ev["strong"] and not is_tail and active(h):
            return False          # what was heard is a live licence: "licensed in the state" alone never moves it
        subs =(len(c) - len(h)) if is_tail else levenshtein(h, c)
        if subs > MAX_SUBS and not ((ev["name"] and ev["hist"]) or ev["name_same"]):
            return False          # two letters changed on a name heard in the reply alone: too far
        if not is_tail and h in at_tx and c in at_tx:
            if at_tx[h] & at_tx[c]:
                return False      # both said in the same transmission: two stations
            adjacent = any(abs(i - j) <= 1 for i in at_tx[h] for j in at_tx[c])
            if not adjacent and not (subs <= 1 and not (active(h) and active(c))):
                return False      # each had its own check-in turn: two stations unless one barely differs
        return True

    def clipped(h: str, banned: set) -> dict | None:
        """A call heard without its first 1-2 characters ("87QF" -> 7QF, "He's 7QX"): completed only when exactly ONE
        Active licence in the station's state ends in that digit + suffix (prefix completion only, no other letter
        changed) AND its licensee's first name is said with the call or in net control's direct reply, or it has
        checked in to this net before. Two or more fit: the one of them heard in full in this net (owner, 11-04) in
        another transmission (the live pass reading the clipped words themselves as a full call is no evidence),
        else left as heard, with the candidates (owner, 11-03)."""
        fits = sorted(c for c in tail_fcc.get(h, []) if active(c) and 1 <= len(c) - len(h) <= MAX_PREFIX
                      and (geo_on or not home_state or (lic.get(c) or {}).get("state") == home_state))
        if not fits:
            return None
        area = f"within {2 * LOCAL_KM} km" if geo_on else f"in {home_state}"
        near = None                                       # VHF/UHF: the clearly nearest licence, if one is
        if geo_on:
            fits.sort(key=lambda c: (dist(c), c))
            d0 = dist(fits[0])
            if d0 <= LOCAL_KM and all(dist(c) >= max(2.5 * d0, d0 + 50) for c in fits[1:]):
                near = fits[0]
        kms = lambda cs: ", ".join(f"{c} {dist(c):.0f} km" for c in cs) if geo_on else ""
        own = {i for i, ev in enumerate(events) if any(k == "tail" and v == h for _, k, v in ev)}
        inn = [c for c in fits if any(v == c and k in ("call", "token", "live")   # heard in full in this net, in a
                                      for i, ev in enumerate(events) if i not in own for _, k, v in ev)]   # turn of its own
        if len(fits) > 1 and len(inn) != 1 and not near:
            return {"call": h, "status": "ambiguous", "conf": 0.0, "candidates": fits[:3],
                    "why": f"{len(fits)} licences {area} end in {h}" + (f" ({kms(fits[:3])})" if geo_on else ""),
                    "score": 0.0}
        same, nxt, said = windows(h, banned)
        if len(fits) > 1 and len(inn) == 1:              # the one of them heard in full in this net (owner, 11-04)
            c = inn[0]
            s, why, ev = evidence(c, same, nxt, said, TAIL_COST)
            s = max(s, MIN_SCORE)
            return {"call": c, "status": "fixed", "conf": _conf(s), "score": s, "_name": ev["name"],
                    "_same": ev["name_same"],
                    "why": "; ".join(why + [f"{len(fits)} licences {area} end in {h}; only {c} was "
                                            f"heard in full in this net"])}
        c = near or fits[0]
        s, why, ev = evidence(c, same, nxt, said, TAIL_COST)
        close = near is not None and dist(near) <= NEAR_KM          # the station's own area: evidence on its own
        if not (ev["name"] or ev["hist"] or close):
            return None if len(fits) == 1 else {"call": h, "status": "ambiguous", "conf": 0.0, "candidates": fits[:3],
                                                "why": f"{len(fits)} licences {area} end in {h} ({kms(fits[:3])})",
                                                "score": 0.0}
        if close and not (ev["name"] or ev["hist"]):
            s = max(s, MIN_SCORE)
        last = (f"the nearest licence ending in {h}" + (f" (then {kms(fits[1:3])})" if len(fits) > 1 else
                                                        f", the only one {area}")) if near else \
            f"the only licence {area} ending in {h}"
        return {"call": c, "status": "fixed", "conf": _conf(s), "score": s, "_name": ev["name"],
                "_same": ev["name_same"], "why": "; ".join(why + [last])}

    def decide(h: str, banned: set) -> dict | None:
        is_tail = h in tails and h not in full
        cands: dict[str, float] = {}
        if is_tail:
            return clipped(h, banned)
        else:
            cands[h] = 0.0
            for c in full:
                if c != h:
                    d = distance(h, c)
                    if d is not None:
                        cands[c] = d
            for c, d in near.get(h, {}).items():
                if c not in cands and active(c):
                    cands[c] = d
            if h in qrz_now and qrz_now[h] != h:
                cands[qrz_now[h]] = 0.0               # the same station: no letters to change
        same, nxt, said = windows(h, banned)
        scored = []
        for c, cost in cands.items():
            s, why, ev = evidence(c, same, nxt, said, cost, h)
            if not allowed(h, c, is_tail, ev):
                continue
            scored.append({"call": c, "score": s, "why": why, "strong": ev["strong"], "name": ev["name"],
                           "same": ev["name_same"]})
        scored.sort(key=lambda x: (-x["score"], x["call"] != h, x["call"]))
        own = next((x for x in scored if x["call"] == h), None)
        if not scored:
            return None
        best = scored[0]
        rest = [x["score"] for x in scored[1:]] + ([own["score"]] if own and own is not best else [])
        second = max(rest) if rest else (0.0 if not is_tail else -1.0)
        if best["call"] != h and best["score"] >= MIN_SCORE and best["score"] - second >= MARGIN:
            return {"call": best["call"], "status": "fixed", "conf": _conf(best["score"] - second),
                    "why": "; ".join(best["why"]), "score": best["score"], "_name": best["name"],
                    "_same": best["same"]}
        if is_tail:
            return None                                  # a clipped call nothing clearly fits: not a call at all
        o = own or {"score": 0.0, "why": ["not in the FCC database"], "strong": False}
        inf = lic.get(h)
        status = ("verified" if (inf and inf["status"] == "A") or o["strong"] else
                  "lapsed" if inf else "unverified")
        far = dist(h) if status == "verified" and not o["strong"] and active(h) else None
        if far is not None and far > reach and in_tx[h] <= 1 and h not in alike:
            status = "distant"      # VHF/UHF: far out of reach, and neither a second hearing nor the other pass backs it
        others = [x["call"] for x in scored if x["call"] != h and x["score"] >= MIN_SCORE
                  and o["score"] - x["score"] < MARGIN][:3]
        e = {"call": h, "status": status, "conf": _conf(o["score"] - max([x["score"] for x in scored
                                                                          if x["call"] != h] + [0.0])),
             "why": "; ".join(o["why"]), "score": o["score"]}
        if status == "distant":
            e["km"] = round(far)
        if others:
            e["candidates"] = others
        tops = [x for x in scored if x["call"] != h and best["score"] - x["score"] < MARGIN]
        if tops and tops[0]["score"] >= MIN_SCORE and tops[0]["score"] - o["score"] >= MARGIN:
            e["_tops"] = tops                           # several readings of one station? settled below
        return e

    heard_all = sorted(set(full) | set(tails))
    bans: dict[str, set] = {h: set() for h in heard_all}
    decided: dict[str, dict] = {}
    todo = list(heard_all)
    for _ in range(6):
        for h in todo:
            e = decide(h, bans[h])
            if e:
                decided[h] = e
            else:
                decided.pop(h, None)
        # one name supports one move: when a name moved several calls to different stations, it stays with the
        # call it was said WITH (then the higher score); the others are judged again without it
        by_name: dict[str, list] = {}
        for h, e in decided.items():
            if e["status"] == "fixed" and e.get("_name"):
                by_name.setdefault(e["_name"], []).append(h)
        todo = []
        for name, hs in by_name.items():
            if len({decided[h]["call"] for h in hs}) < 2:
                continue
            hs.sort(key=lambda h: (not decided[h]["_same"], -decided[h]["score"], h))
            keep = decided[hs[0]]["call"]
            for h in hs[1:]:
                if decided[h]["call"] != keep:
                    bans[h].add(name); todo.append(h)
        if not todo:
            break
    for e in decided.values():
        e.pop("_name", None); e.pop("_same", None)

    # a chain (A -> B, B -> C) ends at C; a call never ends at one that itself moved
    def final(c, seen=()):
        seen = set(seen) | {c}
        while c in decided and decided[c]["call"] != c and decided[c]["call"] not in seen:
            c = decided[c]["call"]; seen.add(c)
        return c
    for h, e in decided.items():
        e["call"] = final(e["call"], {h})
    # close runners-up that are the SAME station read two ways (K7ABQ and KM7ABQ, both -> KM7ABQ): not ambiguous
    for h, e in decided.items():
        tops = e.pop("_tops", None)
        if not tops or e["status"] == "fixed":
            continue
        ends = {final(x["call"]) for x in tops}
        if len(ends) == 1 and h not in ends:
            to = ends.pop()
            also = sorted({x["call"] for x in tops} - {to})
            e.update(call=to, status="fixed", conf=_conf(tops[0]["score"] - e["score"]),
                     why="; ".join(tops[0]["why"]) + (f"; also heard as {', '.join(also)}" if also else ""),
                     score=tops[0]["score"])
            e.pop("candidates", None)

    for h, e in decided.items():                         # another reading moved onto it: heard more than once
        if e["call"] != h and (decided.get(e["call"]) or {}).get("status") == "distant":
            decided[e["call"]]["status"] = "verified"; decided[e["call"]].pop("km", None)

    def confirm(c: str) -> tuple[str | None, str | None]:
        inf = lic.get(c)
        if inf and inf["status"] == "A":
            return inf.get("src") or "FCC", None
        if c in roster:
            return "roster", None
        why = []
        if inf:
            why.append(f"FCC: licence {_STATUS.get(inf['status'], inf['status'])}")
        elif US_CALL.match(c) and fcc is not None:
            why.append("not in the FCC database")
        if c not in qrz:
            why.append("not checked on QRZ.com (offline or no login)")
        elif qrz[c] is None:
            why.append("not on QRZ.com")
        else:
            rec, cur = qrz[c], (qrz[c].get("call") or "").upper()
            if cur and cur != c:
                d = dist(cur)
                why.append(f"QRZ.com: now {cur}" + (f", {d:.0f} km away (out of reach)" if d is not None else ""))
            elif not qrz_current(rec, today):
                why.append(f"QRZ.com: licence expired {rec.get('expdate')}")
        return None, "; ".join(why) or "not in the callbooks"

    for h, e in decided.items():
        src, why = confirm(e["call"])
        e["confirmed"] = src
        if why:
            e["unconfirmed"] = why

    per_tx = {}
    for v, ev in zip(views, events):
        out = []
        for _, k, c in ev:
            if k == "live":
                continue
            e = decided.get(c)
            if k == "call":
                if not (e and e["status"] == "distant"):
                    out.append(e["call"] if e else c)
            elif e and e["status"] == "fixed":           # a token / clipped call only when it was resolved
                out.append(e["call"])
        per_tx[v.get("id")] = list(dict.fromkeys(out))
    return {"calls": decided, "tx": per_tx}


_STATUS = {"E": "expired", "C": "cancelled", "T": "terminated", "L": "pending"}


def _km(a, b) -> float:
    from hamkit.zipgeo import km
    return km(a, b)


def qrz_current(rec: dict, today: str | None) -> bool:
    """A QRZ record whose licence has not expired (no date, or a 0000 date, counts as current: many countries
    publish none)."""
    exp = (rec.get("expdate") or "").strip()
    return not exp or exp.startswith("0000") or not today or exp >= today


def qrz_licence(rec: dict) -> dict:
    """A QRZ record in the shape Licences.info() gives (src says where it came from)."""
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    return {"status": "A", "first": ((rec.get("fname") or "").split() or [None])[0], "city": (rec.get("addr2") or "").upper(),
            "state": (rec.get("state") or "").upper(), "zip": (rec.get("zip") or "")[:5], "src": "QRZ.com",
            "lat": num(rec.get("lat")), "lon": num(rec.get("lon")), "_k": (True, "")}


def qrz_answers(calls, client, lic=None, allow_network: bool = True, budget: int = 40) -> dict:
    """{call: QRZ record | None (QRZ does not know it)} for the calls the FCC does not show Active — the `qrz`
    argument of agree(). Cached answers (found and not found) first; at most `budget` network lookups, and none
    after the first failure (offline: the rest stay unchecked)."""
    if client is None:
        return {}
    calls = [c for c in dict.fromkeys(c.upper() for c in calls) if US_CALL.match(c) or ANY_CALL.match(c)]
    if lic is not None:
        known = lic.info(calls)
        calls = [c for c in calls if (known.get(c) or {}).get("status") != "A"]
    out = {}
    for c in calls:
        st, rec = client.status(c, allow_network=False)
        if st == "unknown" and allow_network and budget > 0:
            budget -= 1
            st, rec = client.status(c, allow_network=True)
            if st == "unknown":
                allow_network = False                   # QRZ unreachable / no login: stop asking this run
        if st != "unknown":
            out[c] = rec
    return out


def _conf(margin: float) -> float:
    return round(1 / (1 + math.exp(-(margin - 0.5))), 2)


# ---------- records kept on each transmission ----------
def records(views: list[dict], result: dict, fix_text=None) -> dict[str, dict]:
    """{tx id: the `consensus` field for that transmission}: its calls after agreement, what moved and why,
    which stay unverified, and (fix_text = text -> (text, changes)) the text after local-name fix-ups. `src`
    ties the record to the text it was made from; a newer accurate text makes it stale (ics.view ignores it)."""
    out = {}
    for v in views:
        tid = v.get("id")
        if v.get("corrected"):
            continue                           # the operator corrected this line: it is evidence, never rewritten
        text = _source_text(v)
        calls = result["tx"].get(tid, [])
        heard_here = list(dict.fromkeys(list(v.get("callsigns") or [])
                                        + [c for _, c, k in _mentions(text, list(v.get("callsigns") or []))
                                           if k != "call"]))
        moved = []
        for h in heard_here:
            e = result["calls"].get(h)
            if e and e["call"] != h:
                moved.append({"heard": h, "call": e["call"], "conf": e["conf"], "why": e["why"]})
        unverified = [c for c in (v.get("callsigns") or []) if (result["calls"].get(c) or {}).get("status")
                      in ("unverified", "lapsed")]
        distant = [{"call": c, "km": result["calls"][c].get("km")} for c in (v.get("callsigns") or [])
                   if (result["calls"].get(c) or {}).get("status") == "distant"]
        rec = {"src": src_hash(text), "callsigns": calls}
        conf = {e["call"]: e for e in result["calls"].values()}
        unconf = [{"call": c, "why": conf[c].get("unconfirmed") or ""} for c in calls
                  if c in conf and not conf[c].get("confirmed")]
        by_qrz = [c for c in calls if (conf.get(c) or {}).get("confirmed") == "QRZ.com"]
        if moved:
            rec["moved"] = moved
        if unverified:
            rec["unverified"] = unverified
        if distant:
            rec["distant"] = distant           # kept in the text, left out of the calls and every count
        if unconf:
            rec["unconfirmed"] = unconf        # counted, but no callbook confirms it (owner, 11-04)
        if by_qrz:
            rec["qrz"] = by_qrz                # confirmed by QRZ.com (the FCC copy does not have it)
        if fix_text:
            t2, ch = fix_text(text)
            if ch and t2 != text:
                rec["text"], rec["fixups"] = t2, ch
        if calls != list(v.get("callsigns") or []) or len(rec) > 2:
            out[tid] = rec                     # nothing to say about a transmission agreement left alone
    return out


def for_session(rows: list[dict], fcc=None, prior=None, home_state=None, roster=(), me="", fix_cfg=None,
                net_names=(), where=None, qrz=None, reach_km=None, net_name=None) -> tuple[dict, dict]:
    """One net's raw transmissions (as kept in the day log) -> ({tx id: consensus record}, agree() result).
    The one entry point for the station (core.Station.agree_session) and the re-run CLI (tools/consensus.py)."""
    from . import fixups, ics
    rows = sorted(rows, key=lambda t: t["start"])
    views = [ics.view(t, agreed=False) for t in rows]
    live = {t["id"]: list(t.get("callsigns") or []) for t in rows if t.get("accurate")}
    res = agree(views, fcc, prior, home_state, roster, me, live, where=where, qrz=qrz, reach_km=reach_km)
    fix = None
    if (fix_cfg and (fix_cfg.get("rules") or fix_cfg.get("local"))) or net_names or (fix_cfg or {}).get("net_names"):
        fix = lambda s: fixups.apply(s, fix_cfg or {}, net_names, net_name)
    return records(views, res, fix), res


def calls_of(tx: dict) -> list[str]:
    """A transmission's callsigns after agreement (the accurate text's, else the live text's)."""
    if tx.get("corrected"):
        from . import ics
        return list(ics.view(tx).get("callsigns") or [])
    base = (tx.get("accurate") or tx).get("callsigns") or []
    rec = tx.get("consensus")
    if not rec:
        return list(base)
    text = (tx.get("accurate") or tx).get("text") or ""
    return apply_view({"text": text, "callsigns": list(base)}, rec)["callsigns"]


def apply_view(v: dict, rec: dict | None) -> dict:
    """A transmission view with its agreement applied: callsigns after agreement, the speaker mapped the same
    way, the fixed-up text (the heard text kept as `heard_text`). A record made from other text is ignored except
    for its call mapping."""
    if not rec:
        return v
    v = dict(v)
    fresh = rec.get("src") == src_hash(v.get("text") or "")
    mapping = {m["heard"]: m["call"] for m in rec.get("moved") or []}
    if fresh:
        v["callsigns"] = list(rec.get("callsigns") or [])
        if rec.get("text"):
            v["heard_text"], v["text"] = v.get("text"), rec["text"]
    else:
        v["callsigns"] = list(dict.fromkeys(mapping.get(c, c) for c in v.get("callsigns") or []))
    far = {d["call"] for d in rec.get("distant") or []}
    if far:
        v["callsigns"] = [c for c in v["callsigns"] if c not in far]
        v["distant"] = list(rec["distant"])
    if v.get("speaker") in mapping and v.get("speaker_how") != "operator":
        v["speaker"] = mapping[v["speaker"]]
    elif v.get("speaker") in far and v.get("speaker_how") != "operator":
        v["speaker"], v["speaker_how"] = None, None   # a speaker out of range and said once: not a check-in
    if rec.get("unverified"):
        v["unverified"] = list(rec["unverified"])
    if rec.get("unconfirmed"):
        v["unconfirmed"] = list(rec["unconfirmed"])
    return v


def report(views: list[dict], result: dict | None = None) -> dict:
    """Distinct calls, calls heard once, and what moved — before (as heard) or after agreement."""
    if result is None:
        per = [list(v.get("callsigns") or []) for v in views]
    else:
        per = [result["tx"].get(v.get("id"), []) for v in views]
    n = Counter(c for calls in per for c in calls)
    return {"distinct": len(n), "once": sum(1 for c in n.values() if c == 1), "mentions": sum(n.values())}
