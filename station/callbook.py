"""One place to ask "is this a real callsign, and who is it?" — FCC (offline), the country file
(offline), QRZ (online, cached for offline). Thread-safe; QRZ network lookups happen off the
transcription path (the caller decides when)."""
import re
import sqlite3
import threading
from pathlib import Path

from hamkit import dxcc, fcc

US = re.compile(r"^(A[A-L]|K|N|W)")


class Callbook:
    def __init__(self, fcc_db: Path = fcc.DEFAULT_DB, cty: Path = dxcc.DEFAULT, qrz=None, roster=()):
        self.lock = threading.Lock()
        self.con = None
        if Path(fcc_db).exists():
            self.con = sqlite3.connect(fcc_db, check_same_thread=False)
        self.cty = str(cty) if Path(cty).exists() else None
        self.qrz = qrz
        self.roster = set(roster)
        self.known: dict[str, bool] = {}

    def fcc(self, call: str) -> dict | None:
        if not self.con:
            return None
        with self.lock:
            return fcc.lookup(self.con, call)

    def country(self, call: str) -> dict | None:
        return dxcc.lookup(call, self.cty) if self.cty else None

    def validate(self, call: str) -> bool:
        """True when the callsign is real enough to show: in the roster, licensed by the FCC, or a
        non-US call whose prefix belongs to a country. A US-shaped call the FCC does not know is
        rejected (Whisper invents those: 'W9XZ' for 'W9XYZ')."""
        c = call.upper()
        if c in self.known:
            return self.known[c]
        base = fcc.base_call(c)
        if c in self.roster or base in self.roster:
            ok = True
        elif US.match(base):
            ok = self.fcc(base) is not None if self.con else True
        else:
            ent = self.country(c)
            ok = bool(ent) and ent["country"] != "United States" and len(base) >= 4
        self.known[c] = ok
        return ok

    def card(self, call: str, qrz_network: bool = False) -> dict:
        c = call.upper()
        card = {"call": c, "sources": []}
        f = self.fcc(c) if US.match(fcc.base_call(c)) else None
        if f:
            card.update(name=f["name"], city=f["city"], state=f["state"], license_class=f["class_name"],
                        license_status=f["status_name"], license_valid=f["valid"], expires=f["expire_date"],
                        club=f["is_club"], trustee=f["trustee_call"], email=f["email"],
                        address=", ".join(x for x in (f["street"] or f["po_box"], f["city"], f["state"], f["zip"]) if x))
            card["sources"].append("FCC")
        ent = self.country(c)
        if ent:
            card.update(country=ent["country"], continent=ent["continent"], cq_zone=ent["cq"], itu_zone=ent["itu"])
            card["sources"].append("country file")
        q = None
        if self.qrz:
            try:
                q = self.qrz.lookup(c, allow_network=qrz_network)
            except Exception as e:  # noqa: BLE001 — QRZ is extra, never required; but say why it is missing
                q, card["qrz_error"] = None, f"{type(e).__name__}: {e}"[:200]
        if q:
            name = " ".join(x for x in (q.get("fname"), q.get("name")) if x)
            for k, v in (("name", name), ("city", q.get("addr2")), ("state", q.get("state")),
                         ("country", q.get("country")), ("email", q.get("email"))):
                if v and not card.get(k):
                    card[k] = v
            card.update(grid=q.get("grid"), image=q.get("image"), lotw=q.get("lotw") == "1",
                        qrz_url=f"https://www.qrz.com/db/{c}")
            card["sources"].append("QRZ")
        if c in self.roster:
            card["roster"] = True
        return card
