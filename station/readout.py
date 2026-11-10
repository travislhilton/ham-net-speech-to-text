"""Read the incident report on a voice net (contracts/emcomm-readout): fetch the EOC's emcomm-readout/1 document,
write the voice script from a fixed template (no AI), have it approved, and show it on a large-type card the
operator reads in their own voice.

Owner 2026-11-10: "in an EMCOMM situation there is a need to read out the current report on the disaster incident on
a voice net"; approval: at a Stake EOC the Stake President or his delegate, at an Area EOC the Area Authority or his
delegate; "Missionary status and injuries should not be read on an open frequency"; the operator reads it in their own
voice (no voice clone); the station keeps refusing to key the transmitter.

Rules this module enforces:
* The document must be emcomm-readout/1 with a matching fingerprint, and must not carry fields about injuries,
  deaths, missionaries or people (names, phones, emails, addresses, GPS): such a document is refused, not filtered.
* Every number in the script is written by code (no AI touches facts); a missing value (null) is "not reported".
* The script itself is checked: no word about injuries, deaths or missionaries can be read.
* An approval binds to the document's fingerprint. A newer document with other facts clears it.
* Each section ends with the station's call ("break"), so the station identifies far more often than every 10
  minutes (97.119); an exercise says "This is an exercise" at the start and end of every section.
* Reading it is logged in the net's minutes (when a net is on) and in data/readouts.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

SCHEMA = "emcomm-readout/1"
DEFAULT_URL = "http://127.0.0.1:8080/api/readout"
REQUIRED = ("schema", "generated_at", "as_of", "fingerprint", "source", "approver", "pace", "incident", "reporting",
            "property", "needs", "supplies", "crews", "volunteers")
ROLE = {"satellite": "Stake President or his delegate", "stake": "Stake President or his delegate",
        "area": "Area Authority or his delegate"}
FORBIDDEN_KEY = re.compile(r"injur|dead|death|deceas|fatal|missionar|submitter|phone|email|address|street|gps|"
                           r"\blat\b|latitude|\blon\b|longitude|coord", re.I)
FORBIDDEN_WORD = re.compile(r"\b(injur\w*|dead|death\w*|deceased|fatalit\w*|killed|missionar\w*|hospital\w*|"
                            r"casualt\w*)\b", re.I)
PACE = {"P": "Primary, cellular", "A": "Alternate, internet", "C": "Contingency, radio",
        "E": "Emergency, runners with paper"}
STALE_MIN = 60

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def words(n) -> str:
    """41 -> forty-one, 74.5 -> seventy-four and a half, 1250 -> one thousand two hundred fifty, None -> not reported."""
    if n is None:
        return "not reported"
    if isinstance(n, float) and not n.is_integer():
        whole, frac = int(n), round(n - int(n), 2)
        if frac == 0.5:
            return f"{words(whole)} and a half"
        return f"{words(whole)} point {' '.join(ONES[int(d)] for d in f'{frac:.2f}'[2:].rstrip('0'))}"
    n = int(n)
    if n < 0:
        return "minus " + words(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("-" + ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return ONES[n // 100] + " hundred" + (" " + words(n % 100) if n % 100 else "")
    for size, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= size:
            return words(n // size) + " " + name + (" " + words(n % size) if n % size else "")
    return str(n)


def count(n, zero="none") -> str:
    return zero if n == 0 else words(n)


def clock_words(iso: str | None) -> str:
    """'2026-11-10T18:57:00-07:00' -> 'eighteen fifty-seven'; 19:00 -> 'nineteen hundred'; 09:05 -> 'zero nine zero five'."""
    if not iso:
        return "not reported"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "not reported"
    h, m = t.hour, t.minute
    hw = ("zero " + ONES[h]) if h < 10 else words(h)
    if m == 0:
        return f"{hw} hundred"
    return f"{hw} {('zero ' + ONES[m]) if m < 10 else words(m)}"


ORD = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth",
       9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth"}


def ordinal(n: int) -> str:
    if n in ORD:
        return ORD[n]
    w = words(n)
    return (w[:-1] + "ieth") if w.endswith("y") else (w + "th")


def spoken_names(text: str) -> str:
    """Names and item descriptions as people say them: acronyms spelled, '4th' -> 'fourth', '12' -> 'twelve'."""
    text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal(int(m.group(1))), text or "")
    text = re.sub(r"\d+(?:\.\d+)?", lambda m: words(float(m.group()) if "." in m.group() else int(m.group())), text)
    text = re.sub(r"\bEOC\b", "E-O-C", text)
    text = re.sub(r"\bWSRM\b", "W-S-R-M", text)
    return text


def item_name(desc: str) -> str:
    """A Church catalog line as people say it: 'Blanket—3-lb. poly fleece (10 per case)' -> 'Blanket, three-pound
    poly fleece'; 'Bottled water (24 bottles per case)' -> 'Bottled water'; "Extension cord, 100'" -> '... one hundred foot'."""
    d = re.sub(r"\s*\([^)]*\)", "", desc or "")
    d = re.sub(r"\s*[—–]\s*", ", ", d)
    d = re.sub(r"(\d+)\s*-?\s*lbs?\.?", r"\1-pound", d)
    d = re.sub(r"(\d+)\s*'", r"\1 foot", d)
    d = re.sub(r'(\d+)\s*"', r"\1 inch", d)
    return spoken_names(d.strip(" ,"))


def joined(names) -> str:
    names = [spoken_names(n) for n in names or []]
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def fingerprint(doc: dict) -> str:
    x = {k: v for k, v in doc.items() if k not in ("generated_at", "fingerprint")}
    return "sha256:" + hashlib.sha256(json.dumps(x, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False).encode("utf-8")).hexdigest()


def _keys(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield f"{path}.{k}", k
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _keys(v, f"{path}[{i}]")


def check(doc) -> dict:
    """Raise ValueError (words for the operator) unless `doc` is a valid emcomm-readout/1 document."""
    if not isinstance(doc, dict):
        raise ValueError("that is not a read-out document")
    sch = str(doc.get("schema") or "")
    if sch != SCHEMA:
        if sch.startswith("emcomm-readout/"):
            raise ValueError(f"this EOC sends a newer report format ({sch}): update the station")
        raise ValueError("that is not an emcomm-readout/1 document")
    missing = [k for k in REQUIRED if k not in doc]
    if missing:
        raise ValueError(f"the report is missing: {', '.join(missing)}")
    bad = [p for p, k in _keys(doc) if FORBIDDEN_KEY.search(k)]
    if bad:
        raise ValueError("refused: the report carries details that are never read on an open frequency "
                         f"({', '.join(bad[:4])})")
    if doc.get("fingerprint") != fingerprint(doc):
        raise ValueError("refused: the report's fingerprint does not match its contents (changed in transit?)")
    lvl = ((doc.get("source") or {}).get("eoc") or {}).get("level")
    if lvl not in ROLE:
        raise ValueError("the report does not say which kind of EOC sent it")
    return doc


def script(doc: dict, call: str, phonetic_call: str) -> list[dict]:
    """The voice script: sections of 30-45 s, each ending with the call (a break for questions)."""
    src, ev = doc["source"], doc["source"]["event"]
    eoc = spoken_names(src["eoc"].get("name") or "the E-O-C")
    ex = bool(ev.get("exercise"))
    rep, inc, pace = doc["reporting"], doc["incident"], doc.get("pace") or {}
    unit = rep.get("unit_word") or "ward"
    units = unit + "s"
    ident = phonetic_call or call
    out = []

    def add(sid, title, text, last=False, wait=False):
        body = text.strip()
        if ex:
            body = "This is an exercise. " + body + " This is an exercise."
        body += (f" {ident}, back to net control." if last else f" {ident}, break.")
        if FORBIDDEN_WORD.search(body):
            raise ValueError(f"section {title}: words about injuries or missionaries are never read")
        n = len(body.split())
        out.append({"id": sid, "title": title, "text": body, "words": n, "seconds": round(n / 2.3),
                    "wait": wait})

    out.append({"id": "callup", "title": "Call-up", "wait": True,
                "text": f"Net control, {ident}, with the {eoc} situation report." + (" This is an exercise." if ex else ""),
                "words": 0, "seconds": 0})
    out[-1]["words"] = len(out[-1]["text"].split())
    out[-1]["seconds"] = round(out[-1]["words"] / 2.3)

    s = f"Situation report from the {eoc}, as of {clock_words(doc.get('as_of'))} local."
    if inc.get("type") or inc.get("area"):
        s += f" Incident: {spoken_names(', '.join(x for x in (inc.get('type'), inc.get('area')) if x))}."
    if pace.get("level") in PACE:
        s += f" Communications are at PACE level {PACE[pace['level']]}."
    exp = rep.get("units_expected")
    if exp is not None:
        s += f" Reports from {words(rep.get('units_reported'))} of {words(exp)} {units}."
        if rep.get("not_reported"):
            s += f" Not yet reported: {joined(rep['not_reported'])}."
    else:
        s += f" Reports from {words(rep.get('units_reported'))} {units if rep.get('units_reported') != 1 else unit}."
    add("bottom", "Bottom line", s)

    pr, nd = doc.get("property") or {}, doc.get("needs") or {}
    s = (f"Damage. Homes damaged: {count(pr.get('homes_damaged'))}. Homes destroyed: {count(pr.get('homes_destroyed'))}."
         f" {units.capitalize()} asking for local Church facilities: {joined(nd.get('local'))}."
         f" Asking for help from neighboring {units}: {joined(nd.get('neighbor'))}."
         f" Asking for Church headquarters support: {joined(nd.get('hq'))}.")
    add("needs", "Damage and needs", s)

    su, cr, vo = doc.get("supplies") or {}, doc.get("crews") or {}, doc.get("volunteers") or {}
    s = f"Supplies. Supply orders: {count(su.get('orders'))}."
    top = [i for i in (su.get("top_items") or []) if i.get("desc")][:3]
    if top:
        s += " Most requested: " + "; ".join(f"{item_name(i['desc'])}, {words(i.get('qty'))}" for i in top) + "."
    s += f" Work crews: {count(cr.get('logs'))}"
    if cr.get("logs"):
        s += f", with {words(cr.get('members'))} members and {words(cr.get('hours'))} hours"
    s += f". Volunteers: {count(vo.get('count'), 'not reported')}"
    if vo.get("count"):
        s += f", {words(vo.get('hours'))} hours"
    s += "."
    add("supplies", "Supplies and crews", s)

    notes = [spoken_names(n) for n in (doc.get("notes") or []) if str(n).strip()]
    if notes:
        add("notes", "Notes from the E-O-C", "Notes from the E-O-C. " + " ".join(
            n if n.endswith(".") else n + "." for n in notes))

    s = ""
    if rep.get("not_reported"):
        s += (f"{joined(rep['not_reported'])}, please send your disaster report to the {eoc} by Winlink, "
              f"or give it on this net. ")
    if inc.get("next_sitrep_at"):
        s += f"Next report at {clock_words(inc['next_sitrep_at'])} local. "
    s += "Questions to net control. End of report."
    add("close", "Asks and close", s, last=True)
    return out


def facts(doc: dict) -> list[tuple[str, str]]:
    """The approver's facts table (what the numbers in the script come from)."""
    r, p, n, s, c, v = (doc.get(k) or {} for k in ("reporting", "property", "needs", "supplies", "crews", "volunteers"))
    show = lambda x: "not reported" if x is None else str(x)
    return [("As of", doc.get("as_of") or "not reported"),
            ("Units reported", f"{show(r.get('units_reported'))} of {show(r.get('units_expected'))}"),
            ("Not yet reported", ", ".join(r.get("not_reported") or []) or "—"),
            ("Reports (DR / SO / WCL)", " / ".join(show((r.get("reports") or {}).get(k)) for k in ("dr", "so", "wcl"))),
            ("Homes damaged / destroyed", f"{show(p.get('homes_damaged'))} / {show(p.get('homes_destroyed'))}"),
            ("Needs: local / neighbor / HQ", " / ".join(", ".join(n.get(k) or []) or "none" for k in ("local", "neighbor", "hq"))),
            ("Supply orders", show(s.get("orders"))),
            ("Work crews (logs, members, hours)", f"{show(c.get('logs'))}, {show(c.get('members'))}, {show(c.get('hours'))}"),
            ("Volunteers (count, hours)", f"{show(v.get('count'))}, {show(v.get('hours'))}")]


def diff(old: dict | None, new: dict) -> list[str]:
    """Facts that changed between two documents (shown when a newer report clears an approval)."""
    if not old:
        return []
    a, b = dict(facts(old)), dict(facts(new))
    return [f"{k}: {a.get(k)} → {b[k]}" for k in b if k != "As of" and a.get(k) != b[k]]


class Readout:
    """The station's read-out: the latest document, its script, its approval, and what was read."""

    def __init__(self, station):
        self.st = station
        self.path = station.data / "readout.json"
        self.log = station.data / "readouts.jsonl"
        self.lock = threading.Lock()
        try:
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.s = {}
        self.s.setdefault("url", DEFAULT_URL)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _append(self, row):
        try:
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _call(self):
        from .agent import phonetic
        call = self.st.me or ""
        return call, phonetic(call) if call else ""

    def load(self, doc: dict, how: str) -> dict:
        check(doc)
        call, ph = self._call()
        sc = script(doc, call, ph)
        with self.lock:
            old = self.s.get("doc")
            same = old and old.get("fingerprint") == doc["fingerprint"]
            changes = [] if same else diff(old, doc)
            if not same and self.s.get("approval"):
                self.s["revoked"] = {"at": self.st.clock().isoformat(timespec="seconds"), "changes": changes,
                                     "by": self.s["approval"].get("by")}
                self.s.pop("approval")
            self.s.update(doc=doc, how=how, loaded_at=self.st.clock().isoformat(timespec="seconds"),
                          error=None, read=self.s.get("read", []) if same else [])
            self._save()
        return self.view()

    def fetch(self, url: str | None = None, opener=urllib.request.urlopen) -> dict:
        url = (url or self.s.get("url") or DEFAULT_URL).strip()
        if not re.match(r"^https?://", url):
            raise ValueError("the EOC address starts with http://")
        try:
            with opener(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=8) as r:
                doc = json.loads(r.read(2_000_000) or b"{}")
        except (OSError, ValueError) as e:
            with self.lock:
                self.s["error"] = f"Couldn't read the EOC at {url}: {type(e).__name__}"
                self._save()
            raise ValueError(self.s["error"]) from None
        with self.lock:
            self.s["url"] = url
        return self.load(doc, f"EOC {url}")

    def approve(self, body: dict) -> dict:
        with self.lock:
            doc = self.s.get("doc")
            if not doc:
                raise ValueError("load a report first")
            if body.get("fingerprint") != doc["fingerprint"]:
                raise ValueError("the report changed while you were reading it: review the new one")
            name = re.sub(r"\s+", " ", str(body.get("by") or "")).strip()[:60]
            if not name:
                raise ValueError("type the approver's name")
            role = ROLE[doc["source"]["eoc"]["level"]]
            if not body.get("confirm_role"):
                raise ValueError(f"the approver must be the {role}")
            self.s["approval"] = {"by": name, "role": role, "fingerprint": doc["fingerprint"],
                                  "at": self.st.clock().isoformat(timespec="seconds")}
            self.s.pop("revoked", None)
            self._save()
        self._append({"approved": self.s["approval"], "eoc": doc["source"]["eoc"], "as_of": doc.get("as_of")})
        return self.view()

    def mark(self, body: dict) -> dict:
        """The operator read a section (or finished): logged, and in the net's minutes at the end."""
        with self.lock:
            doc, ap = self.s.get("doc"), self.s.get("approval")
            if not doc or not ap or ap["fingerprint"] != doc["fingerprint"]:
                raise ValueError("this report is not approved")
            sid = str(body.get("section") or "")
            at = self.st.clock().isoformat(timespec="seconds")
            self.s.setdefault("read", []).append({"section": sid, "at": at, "radio": body.get("radio")})
            self._save()
            done = sid == "close"
        if done:
            self._append({"read": doc["fingerprint"], "at": at, "radio": body.get("radio"), "approved_by": ap["by"]})
            self._minutes(doc, ap, body.get("radio"))
        return self.view()

    def _minutes(self, doc, ap, rid):
        """A minutes item on the net that is on now (on that radio, else any), if there is one."""
        try:
            now = self.st.clock()
            cur = self.st._current_by_radio(now)
            net = cur.get(rid) or next(iter(cur.values()), None)
            if not net or not getattr(self.st, "history", None):
                return
            eoc = doc["source"]["eoc"].get("name") or "the EOC"
            self.st.history.add_item({"inst": net["key"], "kind": "note",
                                      "text": f"Situation report from {eoc} (as of {(doc.get('as_of') or '')[11:16]}) "
                                              f"read on the net; approved by {ap['by']} ({ap['role']})."})
        except Exception as e:  # noqa: BLE001 - the log line is a convenience
            print(f"readout minutes: {type(e).__name__}: {e}", flush=True)

    def set_url(self, url):
        with self.lock:
            self.s["url"] = str(url or DEFAULT_URL).strip()
            self._save()
        return self.view()

    def view(self) -> dict:
        doc, ap = self.s.get("doc"), self.s.get("approval")
        out = {"url": self.s.get("url"), "error": self.s.get("error"), "revoked": self.s.get("revoked"),
               "call": self.st.me, "stale_min": STALE_MIN}
        if not doc:
            return out
        call, ph = self._call()
        try:
            sc = script(doc, call, ph)
        except ValueError as e:
            sc, out["error"] = [], str(e)
        out.update(doc={"fingerprint": doc["fingerprint"], "as_of": doc.get("as_of"), "source": doc["source"],
                        "incident": doc.get("incident"), "exercise": bool(doc["source"]["event"].get("exercise"))},
                   how=self.s.get("how"), loaded_at=self.s.get("loaded_at"), facts=facts(doc), script=sc,
                   role=ROLE[doc["source"]["eoc"]["level"]],
                   approval=ap if ap and ap.get("fingerprint") == doc["fingerprint"] else None,
                   read=self.s.get("read", []),
                   seconds=sum(x["seconds"] for x in sc))
        return out

    def post(self, path: str, body: dict) -> dict:
        """POST /api/readout/fetch {url?}, /load {doc}, /approve {by, confirm_role, fingerprint}, /read {section,
        radio}, /url {url}."""
        what = path.rsplit("/", 1)[-1]
        if what == "fetch":
            return self.fetch(body.get("url"))
        if what == "load":
            return self.load(body.get("doc"), "a file")
        if what == "approve":
            return self.approve(body)
        if what == "read":
            return self.mark(body)
        if what == "url":
            return self.set_url(body.get("url"))
        raise ValueError("unknown read-out request")
