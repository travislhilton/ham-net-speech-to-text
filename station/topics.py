"""Sidekick's prepared topics (a training talk on a net) and the operator's sites (for radio path questions).

Owner 2026-11-12: "after the check-ins the net asks W9XYZ to share the training topic … I then ask W9XYZ Sidekick to
present the topic. The topic might be to explain this project and how to use it … how do we feed the AI assistant
various data that it can analyse … another example: analyze the VHF/UHF path between <a town's> EOC and the churches
which may be used as shelters during a disaster and describe the best way to set up the VHF communications to
support Winlink traffic."

* SITES (data/sites.json, never committed): the places the operator cares about: name, lat/lon, antenna height,
  a note ("EOC", "shelter"). A question that names two of them gets the terrain path computed first
  (hamkit/pathprofile.py), and the model must use those numbers.
* TOPICS (data/topics.json): what to explain, in the operator's words. "Prepare" writes a script from Sidekick's
  library (station/knowledge.py: this project's docs plus the operator's own files, notes and pages) and, for the sites
  ticked, the path table. The script is split into SEGMENTS that each fit one transmission (the radio's transmit time
  limit; a repeater's time-out). The operator reads, edits and APPROVES it before the net; an edit after approval
  un-approves it (the approval is bound to the words).
* ON THE AIR: an approved topic is ARMED on a radio. "Sidekick, present the topic" (or "share the training topic")
  gives segment 1; each segment ends handing the frequency back ("I'll pause there; back to net control"). "Continue"
  / "go ahead" gives the next. The last ends "That concludes the topic." Questions go through the usual answers.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

WORDS_PER_S = 2.4          # a measured, clear speaking pace (about 145 words a minute)
SEGMENT_MAX_S = 55         # default: inside the transmitter's default 60 s limit and any repeater's time-out
SITES_MAX = 60

PRESENT = re.compile(r"\b(present|share|give|start|begin|go ahead with|do)\b[\w\s,']{0,30}\b(topic|training|"
                     r"presentation|talk|lesson)\b", re.I)
CONTINUE = re.compile(r"\b(continue|go on|go ahead|keep going|next (part|segment|section)|carry on|proceed)\b", re.I)


# ---------------------------------------------------------------- sites ------------------------------------------
def parse_sites(text: str) -> list[dict]:
    """One site a line: "Name | lat, lon | antenna height (ft or m) | note". The note and height are optional."""
    out = []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            raise ValueError(f"line {n}: name | lat, lon | height | note")
        m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", parts[1])
        if not m:
            raise ValueError(f"line {n}: the place must be lat, lon in decimal degrees (e.g. 39.80, -89.65)")
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"line {n}: lat, lon out of range")
        h_m = 6.0
        if len(parts) > 2 and parts[2]:
            hm = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ft|feet|'|m|meters?)?", parts[2], re.I)
            if not hm:
                raise ValueError(f"line {n}: antenna height like 30 ft or 10 m")
            v, unit = float(hm.group(1)), (hm.group(2) or "ft").lower()
            h_m = v * 0.3048 if unit in ("ft", "feet", "'") else v
        out.append({"name": parts[0][:60], "lat": lat, "lon": lon, "height_m": round(h_m, 1),
                    "note": (parts[3] if len(parts) > 3 else "")[:120]})
    if len(out) > SITES_MAX:
        raise ValueError(f"at most {SITES_MAX} sites")
    return out


def sites_text(sites: list[dict]) -> str:
    return "\n".join(f"{s['name']} | {s['lat']:.5f}, {s['lon']:.5f} | {round(s['height_m'] / 0.3048)} ft"
                     + (f" | {s['note']}" if s.get("note") else "") for s in sites)


def sites_named(text: str, sites: list[dict]) -> list[dict]:
    """The sites a question names (by their name, case-insensitive), in the order named."""
    t = (text or "").lower()
    hits = [(t.find(s["name"].lower()), s) for s in sites if s["name"] and s["name"].lower() in t]
    return [s for _, s in sorted(hits, key=lambda x: x[0])]


def path_facts(sites: list[dict], mhz: float = 146.0, analyze=None, max_pairs: int = 6) -> list[str]:
    """Terrain path results between the sites (every pair, up to max_pairs), as lines for a model."""
    if len(sites) < 2:
        return []
    if analyze is None:
        from hamkit import pathprofile
        analyze = lambda a, b: pathprofile.analyze(                       # noqa: E731
            pathprofile.Point(a["lat"], a["lon"], a["height_m"], a["name"]),
            pathprofile.Point(b["lat"], b["lon"], b["height_m"], b["name"]), mhz, source="open-meteo")
    out = [f"Terrain path analysis at {mhz:g} MHz (computed by the station from elevation data; authoritative: use "
           f"these numbers, do not estimate your own):"]
    n = 0
    for i, a in enumerate(sites):
        for b in sites[i + 1:]:
            if n >= max_pairs:
                out.append("(more pairs not computed)")
                return out
            n += 1
            try:
                r = analyze(a, b)
                out.append(f"- {a['name']} to {b['name']}: " + str(r.get("text") or r.get("verdict") or "").replace(
                    "\n", " ")[:600])
            except Exception as e:  # noqa: BLE001 - offline / no data: said, never invented
                out.append(f"- {a['name']} to {b['name']}: not computed ({type(e).__name__}: {str(e)[:80]})")
    return out


# ---------------------------------------------------------------- topics -----------------------------------------
def segment(script: str, max_words: int) -> list[str]:
    """Split a script into transmissions of at most max_words, at sentence ends (a paragraph break is a natural
    place). A sentence longer than a whole segment is cut at a comma or, last, at the word limit."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", script or "") if p.strip()]
    sents = []
    for p in paras:
        ss = [x.strip() for x in re.split(r"(?<=[.!?])\s+", p) if x.strip()]
        sents += ss + ["\n"]
    segs, cur = [], []
    for s in sents:
        if s == "\n":
            if cur and len(" ".join(cur).split()) > max_words * 0.6:
                segs.append(" ".join(cur))
                cur = []
            continue
        words = s.split()
        while len(words) > max_words:                       # one long sentence: cut it
            if cur:
                segs.append(" ".join(cur))
                cur = []
            segs.append(" ".join(words[:max_words]))
            words = words[max_words:]
        if len(" ".join(cur).split()) + len(words) > max_words and cur:
            segs.append(" ".join(cur))
            cur = []
        cur.append(" ".join(words))
    if cur:
        segs.append(" ".join(cur))
    return [x for x in segs if x.strip()]


def fingerprint(segs: list[str]) -> str:
    return hashlib.sha1("\n".join(segs).encode()).hexdigest()[:16]


TOPIC_SYSTEM = """You are {name}, the automated assistant of amateur radio station {call} (control operator {op}). \
The control operator will ask you, on the air, to present a short training topic to a net. Write the spoken script.

The topic, in the operator's words: {request}

Rules:
- Spoken English for radio: short sentences, no lists or bullet points, no URLs, no tables, numbers as a person says
  them, standard phonetics for call signs. Plain language, no codes or abbreviations a listener might not know (say
  "emergency operations center" the first time, then EOC).
- Use ONLY the reference material and computed results given below for facts about this project, the operator's
  sites, equipment and procedures. Where the material does not say, say so plainly ("{op} can tell you more") rather
  than guess. Numbers from the computed path analysis are authoritative.
- About {minutes} minutes in all ({words} words at most). Paragraphs: each paragraph is about one idea and is said in
  one transmission of at most {seg_words} words.
- Begin: "This is {call}, {name}, an automated assistant; {op} is control operator. Today's topic: <title>."
- Do not end paragraphs with the call sign or hand-offs: the station adds them.
Return JSON: title (short), script (the paragraphs separated by blank lines), sources (the reference labels used)."""

TOPIC_SCHEMA = {"type": "object",
                "properties": {"title": {"type": "string"}, "script": {"type": "string"},
                               "sources": {"type": "array", "items": {"type": "string"}}},
                "required": ["title", "script", "sources"], "additionalProperties": False}


class Topics:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "topics.json"
        self.sites_path = Path(data_dir) / "sites.json"
        self.lock = threading.Lock()
        self.doc = self._load(self.path, {"topics": []})
        self.sites = self._load(self.sites_path, {"sites": []}).get("sites") or []
        self.armed: dict[str, dict] = {}            # radio -> {"topic": id, "next": index}

    @staticmethod
    def _load(p, default):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.doc, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def set_sites(self, text: str) -> list[dict]:
        sites = parse_sites(text)
        with self.lock:
            self.sites = sites
            tmp = self.sites_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sites": sites}, indent=1), encoding="utf-8")
            tmp.replace(self.sites_path)
        return sites

    def get(self, tid: str) -> dict:
        t = next((x for x in self.doc["topics"] if x["id"] == tid), None)
        if not t:
            raise LookupError("no such topic")
        return t

    def upsert(self, body: dict) -> dict:
        with self.lock:
            tid = body.get("id")
            t = self.get(tid) if tid else None
            if t is None:
                t = {"id": uuid.uuid4().hex[:8], "created": datetime.now().isoformat(timespec="seconds"),
                     "segments": [], "approved": None}
                self.doc["topics"].append(t)
            for k in ("request", "title", "minutes", "sites", "mhz"):
                if k in body:
                    t[k] = body[k]
            t["request"] = str(t.get("request") or "")[:2000]
            t["title"] = str(t.get("title") or t["request"][:60] or "Topic")[:80]
            t["minutes"] = max(1, min(15, int(t.get("minutes") or 4)))
            t["sites"] = [str(x)[:60] for x in (t.get("sites") or [])][:12]
            if "segments" in body:                          # the operator's edits
                segs = [re.sub(r"\s+", " ", str(x)).strip() for x in body["segments"] if str(x).strip()]
                t["segments"] = segs[:40]
            if t.get("approved") and t["approved"] != fingerprint(t["segments"]):
                t["approved"] = None                        # the words changed: approve again
            t["updated"] = datetime.now().isoformat(timespec="seconds")
            self._save()
            return t

    def remove(self, tid: str):
        with self.lock:
            self.doc["topics"] = [x for x in self.doc["topics"] if x["id"] != tid]
            self.armed = {r: a for r, a in self.armed.items() if a["topic"] != tid}
            self._save()

    def approve(self, tid: str, on: bool = True) -> dict:
        with self.lock:
            t = self.get(tid)
            if on and not t.get("segments"):
                raise ValueError("prepare the script first")
            t["approved"] = fingerprint(t["segments"]) if on else None
            self._save()
            return t

    def arm(self, rid: str, tid: str | None):
        with self.lock:
            if not tid:
                self.armed.pop(rid, None)
                return None
            t = self.get(tid)
            if t.get("approved") != fingerprint(t.get("segments") or []):
                raise ValueError("approve the script first (the words you approve are the words it says)")
            self.armed[rid] = {"topic": tid, "next": 0}
            return self.armed[rid]

    def view(self) -> dict:
        return {"topics": self.doc["topics"], "sites": self.sites, "sites_text": sites_text(self.sites),
                "armed": self.armed, "segment_max_s": SEGMENT_MAX_S}

    def next_segment(self, rid: str) -> tuple[dict, int, str] | None:
        """(topic, index, words) of the next segment to say on this radio, or None."""
        a = self.armed.get(rid)
        if not a:
            return None
        try:
            t = self.get(a["topic"])
        except LookupError:
            return None
        if t.get("approved") != fingerprint(t.get("segments") or []) or a["next"] >= len(t["segments"]):
            return None
        return t, a["next"], t["segments"][a["next"]]

    def said(self, rid: str, tid: str, i: int):
        a = self.armed.get(rid)
        if a and a["topic"] == tid and a["next"] == i:
            a["next"] = i + 1

    # ----- preparing a script -----
    def prepare(self, tid: str, ask, library=None, ctx: dict | None = None, max_s: int = SEGMENT_MAX_S,
                analyze=None) -> dict:
        """Write the script with the thinking model: `ask(system, user)` -> {"title", "script", "sources"}."""
        t = self.get(tid)
        ctx = ctx or {}
        seg_words = max(40, int((min(max_s, 180) - 5) * WORDS_PER_S))
        words = int(t["minutes"] * 60 * WORDS_PER_S)
        material = library.outline(t["request"], 14000) if library else ""
        chosen = [s for s in self.sites if s["name"] in (t.get("sites") or [])]
        paths = path_facts(chosen, float(t.get("mhz") or 146.0), analyze=analyze) if len(chosen) > 1 else []
        site_lines = [f"- {s['name']}: {s['lat']:.4f}, {s['lon']:.4f}, antenna {round(s['height_m'] / 0.3048)} ft"
                      + (f" ({s['note']})" if s.get("note") else "") for s in chosen]
        system = TOPIC_SYSTEM.format(name=ctx.get("name", "Sidekick"), call=ctx.get("call", "N0CALL"),
                                     op=ctx.get("op", "the operator"), request=t["request"], minutes=t["minutes"],
                                     words=words, seg_words=seg_words)
        user = "\n".join([*(["The operator's sites:", *site_lines] if site_lines else []), *paths,
                          "Reference material (from the station's library; labels in brackets):",
                          material or "(nothing in the library matches: say only what is general knowledge, "
                                      "and say that the operator can give details)"])
        out = ask(system, user)
        segs = segment(str(out.get("script") or ""), seg_words)
        if not segs:
            raise ValueError("the model wrote no script")
        call, name = ctx.get("call", "N0CALL"), ctx.get("name", "Sidekick")
        for i in range(len(segs)):                          # the hand-offs and the ID, in code
            last = i == len(segs) - 1
            segs[i] = segs[i].rstrip() + (f" That concludes the topic. Back to net control. {call}." if last else
                                          f" I'll pause there. Back to net control. {call}.")
        return self.upsert({"id": tid, "title": str(out.get("title") or t["title"])[:80], "segments": segs}) | {
            "sources": out.get("sources") or [], "paths": paths}
