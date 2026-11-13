"""Local names (Settings → Local names): the clubs, repeaters, nets and web sites of the station's own area, which
Whisper writes as ordinary words ("the Sark Link repeater", "SarkNet", "go to SarkOnline" for a club whose name is
said as a word). Each entry becomes repair rules applied AFTER transcription, in the net-wide agreement pass
(station/consensus.py → station/fixups.py), so the heard text is kept beside the repaired one and a later change of
the list re-runs cleanly. They are never put in Whisper's prompt (a prompt full of local names made Whisper echo it).

An entry (data/local_names.json, station-local: real names never go in the repo):

    {"name": "SARC", "meaning": "Springfield Amateur Radio Club", "said": "word" | "letters",
     "sounds": ["sark"],                       # how Whisper writes it (said as a word: its lower case is added)
     "also": [{"heard": "Sarkowitz Repair", "say": "SARC Link repeater"}],   # whole phrases, exactly
     "web": "sarconline.com",                  # optional: the club's site, often said aloud ("go to Sark online")
     "nets": []}                               # net names it applies to (as on the schedule); empty = every net

Entries name their nets by NAME (not by a station's net ids) so an AREA PACK (pack()/unpack(): the area's local names
in one file, published by someone in the area, imported by the installer or Settings) works on any station.

The rules, in this order:
  1. `also` phrases, as written (whole words, any case).
  2. the web site said aloud: "Sark online", "SarkOnline", "sark online dot com" -> sarconline.com
  3. a sound-alike joined to a capitalised word: "SarkNet" -> "SARC Net"
  4. the sound-alike alone -> SARC, only when it reads as a name: written with a capital away from the start of a
     sentence, or with a radio/club word right beside it ("the sark net", "sark link", "the sark web page").
     "Meet at the park after the net" stays as it is.
  5. said letter by letter: "S A R C" / "S.A.R.C." -> SARC (no guard: a spelled run is never an ordinary word).
"""
import json
import os
import re
from pathlib import Path

FILE = "local_names.json"
MAX_ENTRIES = 200
RADIO_WORDS = re.compile(r"\b(nets?|link|linked|repeaters?|systems?|club|online|web ?pages?|web ?sites?|site|roster|"
                         r"meetings?|members?|frequency|machines?|group|hamfest|newsletter|board|president|"
                         r"check[- ]?ins?|ares|races|emcomm)\b", re.I)
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 &'.-]{0,39}")
WEB_RE = re.compile(r"(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})/?", re.I)


# ---------- the list ----------
def path(data_dir) -> Path:
    return Path(data_dir) / FILE


def load(data_dir) -> list[dict]:
    try:
        d = json.loads(path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for e in (d.get("names") if isinstance(d, dict) else d) or []:
        try:
            out.append(validate(e))
        except ValueError:
            continue
    return out


def save(data_dir, entries, area: str = "") -> list[dict]:
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError(f"at most {MAX_ENTRIES} local names")
    clean = [validate(e) for e in entries]
    seen = set()
    for e in clean:
        if e["name"].lower() in seen:
            raise ValueError(f"{e['name']} is in the list twice")
        seen.add(e["name"].lower())
    p = path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"area": " ".join(str(area or "").split())[:80], "names": clean}, indent=1,
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return clean


def _words(v, most, longest=40) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out = []
    for s in v or []:
        s = " ".join(str(s).split())
        if s and len(s) <= longest and s not in out:
            out.append(s)
    return out[:most]


def validate(e) -> dict:
    if not isinstance(e, dict):
        raise ValueError("a local name is an object")
    name = " ".join(str(e.get("name") or "").split())
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"name {name!r}: letters, digits, spaces and & ' . - (up to 40)")
    said = e.get("said") or ("letters" if re.fullmatch(r"[A-Z]{2,6}", name) and not e.get("sounds") else "word")
    if said not in ("word", "letters"):
        raise ValueError("said: word or letters")
    web = str(e.get("web") or "").strip()
    if web:
        m = WEB_RE.fullmatch(web)
        if not m:
            raise ValueError(f"{name}: web site {web!r} is not a web address like example.org")
        web = m.group(1).lower()
    also = []
    for a in e.get("also") or []:
        if isinstance(a, str) and "=" in a:
            a = dict(zip(("heard", "say"), (x.strip() for x in a.split("=", 1))))
        if isinstance(a, dict) and str(a.get("heard") or "").strip() and str(a.get("say") or "").strip():
            h, s = " ".join(str(a["heard"]).split()), " ".join(str(a["say"]).split())
            if len(h) > 60 or len(s) > 60:
                raise ValueError(f"{name}: an 'also heard' phrase is at most 60 characters")
            also.append({"heard": h, "say": s})
    nets = _words(e.get("nets"), 40, longest=80)
    return {"name": name, "meaning": " ".join(str(e.get("meaning") or "").split())[:160], "said": said,
            "sounds": _words(e.get("sounds"), 12), "also": also[:20], "web": web or None, "nets": nets}


# ---------- rules ----------
def _applies(e, net_name) -> bool:
    return not e["nets"] or bool(net_name and net_name.strip().lower() in {n.lower() for n in e["nets"]})


def _alt(words) -> str:
    """Sound-alikes as one regex alternative: spaces may be spaces or hyphens or nothing ("sark net")."""
    return "|".join(r"[\s-]?".join(map(re.escape, w.split())) for w in sorted(words, key=len, reverse=True))


def rules(entries, net_name=None) -> list[dict]:
    """The compiled rules for the entries that apply on the net called `net_name` (None = outside a scheduled net:
    only the entries for every net)."""
    out = []
    for e in entries or []:
        if not _applies(e, net_name):
            continue
        name = e["name"]
        sounds = [s for s in e["sounds"] if s.lower() != name.lower()]
        if e["said"] == "word":
            sounds = [name.lower()] + [s for s in sounds if s.lower() != name.lower()]
        for a in sorted(e["also"], key=lambda a: -len(a["heard"])):
            ws = re.findall(r"[\w'.]+", a["heard"])
            if ws:
                out.append({"re": re.compile(r"(?<![\w-])" + r"[\s,;:-]+".join(map(re.escape, ws)) + r"(?![\w-])", re.I),
                            "say": a["say"], "kind": "also", "name": name})
        if e["web"]:
            host = e["web"]
            base, tld = host.rsplit(".", 1)
            # whatever ending was said or written goes with it ("Sarkonline.org" when the list says .com would
            # otherwise read "sarconline.com.org"); the list's address is the one written
            spoken_tld = r"(?:\s*(?:dot|\.)\s*(?:" + re.escape(tld) + r"|com|org|net|us|info|club))?"
            if sounds and base.lower().startswith(name.lower()) and len(base) > len(name):
                rest = base[len(name):]
                out.append({"re": re.compile(r"(?<![\w.-])(?:" + _alt(sounds + [name]) + r")[\s-]?" + re.escape(rest)
                                             + spoken_tld + r"(?![\w-])", re.I),
                            "say": host, "kind": "web", "name": name})
            out.append({"re": re.compile(r"(?<![\w.-])" + re.escape(base) + r"\s*(?:dot|\.)\s*(?:" + re.escape(tld)
                                         + r"|com|org|net|us|info|club)(?![\w-])", re.I), "say": host, "kind": "web",
                        "name": name})
        if sounds:
            alt = _alt(sounds)
            out.append({"re": re.compile(r"(?<![\w-])(?i:" + alt + r")(?P<tail>[A-Z][a-z]{1,14})(?![\w-])"),
                        "say": name + " {tail}", "kind": "joined", "name": name})
            out.append({"re": re.compile(r"(?<![\w-])(?:" + alt + r")(?![\w-])", re.I), "say": name, "kind": "word",
                        "name": name, "guard": True})
        if e["said"] == "letters" or re.fullmatch(r"[A-Z]{2,8}", name):
            letters = [c for c in name if c.isalnum()]
            if len(letters) >= 2:
                out.append({"re": re.compile(r"(?<![\w-])" + r"[\s.-]+".join(map(re.escape, letters)) + r"\.?(?![\w-])",
                                             re.I), "say": name, "kind": "letters", "name": name})
                out.append({"re": re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.I), "say": name,
                            "kind": "case", "name": name})
    return out


def _sentence(text: str, i: int) -> tuple[int, int]:
    s = max(text.rfind(".", 0, i), text.rfind("?", 0, i), text.rfind("!", 0, i)) + 1
    ends = [j for j in (text.find(".", i), text.find("?", i), text.find("!", i)) if j != -1]
    return s, (min(ends) if ends else len(text))


def _reads_as_name(text: str, m: re.Match) -> bool:
    """A capital away from the start of a sentence (Whisper heard a name), or a radio/club word right beside it
    ("the sark net", "sark link", "the Sark web page"). A radio word elsewhere in the sentence is not enough: net
    talk is full of "net" ("we met at the park after the net")."""
    s, _ = _sentence(text, m.start())
    word = m.group(0)
    starts = not text[s:m.start()].strip(" \"'(,-")
    if word[:1].isupper() and not starts:
        return True
    after = re.match(r"[\s-]*([A-Za-z]+(?:[\s-][A-Za-z]+)?)", text[m.end():])
    before = re.search(r"([A-Za-z]+)[\s-]*$", text[:m.start()])
    nxt = after.group(1) if after else ""
    return bool(RADIO_WORDS.fullmatch(nxt) or RADIO_WORDS.fullmatch(nxt.split()[0] if nxt else "")
                or (before and RADIO_WORDS.fullmatch(before.group(1))))


def apply(text: str, rules_: list[dict]) -> tuple[str, list[dict]]:
    """-> (text, changes [{"heard", "say", "why"}])."""
    if not text or not rules_:
        return text, []
    changes = []
    for r in rules_:
        src = text

        def sub(m, r=r, src=src):
            if r.get("guard") and not _reads_as_name(src, m):
                return m.group(0)
            new = r["say"].format(**m.groupdict()) if "{" in r["say"] else r["say"]
            if new != m.group(0):
                changes.append({"heard": m.group(0), "say": new, "why": f"local name ({r['name']})"})
            return new
        text = r["re"].sub(sub, text)
    return text, changes


def terms_in(texts, entries) -> list[dict]:
    """The entries named in these texts (after repair), in the order first named: for the ICS-213's Local terms."""
    joined = "\n".join(t for t in texts if t)
    hits = []
    for e in entries or []:
        pats = [r"(?<![\w-])" + re.escape(e["name"]) + r"(?![\w-])"] + ([re.escape(e["web"])] if e["web"] else [])
        pos = [m.start() for p in pats for m in [re.search(p, joined)] if m]
        if pos:
            hits.append((min(pos), e))
    return [e for _, e in sorted(hits, key=lambda x: x[0])]


def footer(entries) -> str:
    if not entries:
        return ""
    lines = [f"  {e['name']}" + (f": {e['meaning']}" if e["meaning"] else "") + (f" ({e['web']})" if e["web"] else "")
             for e in entries]
    return "Local terms:\n" + "\n".join(lines)


# ---------- area packs ----------
PACK_SCHEMA = "hamnet-area/1"   # a file-format identifier of area packs already shared: not renamed


def pack(entries, area: str = "") -> dict:
    """The area pack: what another station in the area imports (Settings → Local names → Import, or the installer's
    -AreaPack). No callsigns, no operator details: only the area's public names."""
    return {"schema": PACK_SCHEMA, "area": " ".join(str(area or "").split())[:80],
            "local_names": [validate(e) for e in entries]}


def unpack(doc, have=()) -> tuple[list[dict], dict]:
    """An area pack (or a bare list) merged into the station's list `have`: a name already there is replaced by the
    pack's entry. -> (merged list, {"added": n, "updated": n, "area": ...})."""
    if isinstance(doc, dict):
        if doc.get("schema") not in (None, PACK_SCHEMA):
            raise ValueError(f"not an area pack ({doc.get('schema')!r}); expected {PACK_SCHEMA}")
        incoming, area = doc.get("local_names") or [], doc.get("area") or ""
    elif isinstance(doc, list):
        incoming, area = doc, ""
    else:
        raise ValueError("an area pack is a JSON object with local_names")
    new = [validate(e) for e in incoming]
    out = [dict(e) for e in have]
    pos = {e["name"].lower(): i for i, e in enumerate(out)}
    added = updated = 0
    for e in new:
        i = pos.get(e["name"].lower())
        if i is None:
            pos[e["name"].lower()] = len(out); out.append(e); added += 1
        elif out[i] != e:
            out[i] = e; updated += 1
    if len(out) > MAX_ENTRIES:
        raise ValueError(f"at most {MAX_ENTRIES} local names")
    return out, {"added": added, "updated": updated, "area": area}


def fetch(url: str, opener=None, limit=1_000_000) -> dict:
    """An area pack from a web address (https only, or http on the local network)."""
    import urllib.request
    if not re.match(r"^https://|^http://(127\.|10\.|192\.168\.|localhost)", url):
        raise ValueError("an area pack's address starts with https://")
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                                timeout=15) as r:
            return json.loads(r.read(limit) or b"{}")
    except (OSError, ValueError) as e:
        raise ValueError(f"could not read the area pack at {url}: {type(e).__name__}") from e


def main(argv=None) -> int:
    """python -m station.localnames import <file-or-url> [--data DIR]   (the installer's -AreaPack)
       python -m station.localnames export <file> [--data DIR]"""
    import argparse
    ap = argparse.ArgumentParser(description="Local names: import or export an area pack")
    ap.add_argument("action", choices=["import", "export"])
    ap.add_argument("where")
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent.parent / "data"))
    a = ap.parse_args(argv)
    if a.action == "export":
        try:
            area = json.loads(path(a.data).read_text(encoding="utf-8")).get("area") or ""
        except (OSError, ValueError, AttributeError):
            area = ""
        Path(a.where).write_text(json.dumps(pack(load(a.data), area), indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"exported {len(load(a.data))} local names to {a.where}")
        return 0
    doc = fetch(a.where) if re.match(r"^https?://", a.where) else json.loads(Path(a.where).read_text(encoding="utf-8-sig"))
    try:
        area = json.loads(path(a.data).read_text(encoding="utf-8")).get("area") or ""
    except (OSError, ValueError, AttributeError):
        area = ""
    names, note = unpack(doc, load(a.data))
    save(a.data, names, area or note["area"])
    print(f"local names: {note['added']} added, {note['updated']} updated from {a.where}"
          + (f" ({note['area']})" if note["area"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
