"""QRZ.com XML Logbook Data lookups with an offline cache.

QRZ has no API key for callsign lookups (the Logbook API's access key only reaches your own
logbook): the XML service takes the account username + password and returns a session key, which
is reused until it expires. The password is kept in the OS credential store (Windows Credential
Manager / macOS Keychain) via `python -m hamkit.qrz set-login` (hidden prompt). Fallbacks, in
order: QRZ_USERNAME / QRZ_PASSWORD in the environment, then config/local.yaml qrz.username/password. Every successful lookup is cached in
<CALLBOOK_DIR>/qrz_cache.sqlite, so a call looked up once is available with no network.
W9XYZ's account: QRZ Premium with XML Logbook Data enabled (checked 2026-11-02, ends 2027-01-24).

    python -m hamkit.qrz set-login      # store username + password in the OS credential store
    python -m hamkit.qrz check          # session status: subscription expiry, lookups today
    python -m hamkit.qrz lookup EA8AB
"""
import json, os, sqlite3, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

URL = "https://xmldata.qrz.com/xml/current/"
AGENT = "ham-net-speech-to-text-0.1"
ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("CALLBOOK_DIR", Path.home() / "data" / "callbook")) / "qrz_cache.sqlite"
TTL_S = 30 * 86400
FIELDS = ["call", "aliases", "dxcc", "fname", "name", "nickname", "addr1", "addr2", "state", "zip", "country",
          "lat", "lon", "grid", "county", "class", "efdate", "expdate", "email", "url", "image", "qslmgr",
          "eqsl", "lotw", "mqsl", "born", "user", "geoloc", "cqzone", "ituzone", "land", "trustee"]


class QRZError(RuntimeError):
    pass


KEYRING_SERVICE = "ham-net-speech-to-text/qrz"


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:  # noqa: BLE001 — no keyring backend: fall through to env / file
        return None


def credentials() -> tuple[str, str] | None:
    kr = _keyring()
    if kr:
        try:
            u = kr.get_password(KEYRING_SERVICE, "username")
            p = u and kr.get_password(KEYRING_SERVICE, u)
            if u and p:
                return u, p
        except Exception:  # noqa: BLE001
            pass
    u, p = os.environ.get("QRZ_USERNAME"), os.environ.get("QRZ_PASSWORD")
    if u and p:
        return u, p
    f = ROOT / "config" / "local.yaml"
    if f.exists():
        q = (yaml.safe_load(f.read_text()) or {}).get("qrz") or {}
        if q.get("username") and q.get("password"):
            return str(q["username"]), str(q["password"])
    return None


def _strip_ns(root):
    for el in root.iter():
        el.tag = el.tag.split("}", 1)[-1]
    return root


def parse(xml_bytes: bytes) -> tuple[dict, dict | None]:
    """(session, callsign-record or None) from one QRZ response."""
    root = _strip_ns(ET.fromstring(xml_bytes))
    s = root.find("Session")
    session = {c.tag: (c.text or "").strip() for c in s} if s is not None else {}
    c = root.find("Callsign")
    rec = {f: (c.findtext(f) or "").strip() or None for f in FIELDS} if c is not None else None
    return session, rec


class Client:
    def __init__(self, creds=None, cache: Path = CACHE, fetch=None):
        self.creds = creds if creds is not None else credentials()
        self.key = None
        self.fetch = fetch or self._http
        cache.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cache)
        self.db.execute("CREATE TABLE IF NOT EXISTS qrz (call TEXT PRIMARY KEY, fetched REAL, rec TEXT)")

    @staticmethod
    def _http(params: dict) -> bytes:
        q = urllib.parse.urlencode(params)
        with urllib.request.urlopen(f"{URL}?{q}", timeout=15) as r:
            return r.read()

    def login(self) -> dict:
        if not self.creds:
            raise QRZError("no QRZ credentials (config/local.yaml qrz.username/password)")
        s, _ = parse(self.fetch({"username": self.creds[0], "password": self.creds[1], "agent": AGENT}))
        if not s.get("Key"):
            raise QRZError(s.get("Error") or "login failed")
        self.key = s["Key"]
        return s

    def cached(self, call: str) -> dict | None:
        r = self.db.execute("SELECT fetched, rec FROM qrz WHERE call=?", (call.upper(),)).fetchone()
        if not r:
            return None
        d = json.loads(r[1]); d["cached_at"] = r[0]; d["stale"] = time.time() - r[0] > TTL_S
        return d

    def lookup(self, call: str, allow_network: bool = True) -> dict | None:
        call = call.upper().strip()
        hit = self.cached(call)
        if hit and not hit["stale"] or not allow_network or not self.creds:
            return hit
        try:
            for attempt in (1, 2):
                if not self.key:
                    self.login()
                s, rec = parse(self.fetch({"s": self.key, "callsign": call}))
                err = s.get("Error", "")
                if rec:
                    rec["source"] = "QRZ.com"
                    self.db.execute("INSERT OR REPLACE INTO qrz VALUES (?,?,?)", (call, time.time(), json.dumps(rec)))
                    self.db.commit()
                    return rec
                if "Not found" in err:
                    return None
                if "Session Timeout" in err or "Invalid session key" in err:
                    self.key = None; continue
                raise QRZError(err or "no record")
        except (OSError, ET.ParseError):
            return hit                                   # offline / QRZ down: the cache is the answer
        return hit


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if a[:1] == ["set-login"]:
        import getpass
        kr = _keyring()
        if not kr:
            print("no OS credential store available; use config/local.yaml"); return 1
        u = input("QRZ username (your callsign): ").strip().upper()
        p = getpass.getpass("QRZ password (hidden): ")
        kr.set_password(KEYRING_SERVICE, "username", u); kr.set_password(KEYRING_SERVICE, u, p)
        print(f"stored for {u}; testing...")
        a = ["check"]
    c = Client()
    if a[:1] == ["check"]:
        s = c.login()
        print(json.dumps({k: s.get(k) for k in ("SubExp", "Count", "GMTime", "Message", "Error")}))
    elif a[:1] == ["lookup"]:
        for call in a[1:]:
            print(json.dumps(c.lookup(call)))
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
