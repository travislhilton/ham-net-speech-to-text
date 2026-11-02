"""hamkit.qrz with a fake QRZ server: login, lookup, cache, session expiry, offline."""
from hamkit import qrz

LOGIN = b"""<?xml version="1.0"?><QRZDatabase version="1.34" xmlns="http://xmldata.qrz.com"><Session>
<Key>abc123</Key><Count>5</Count><SubExp>Sun Jan 24 00:00:00 2027</SubExp></Session></QRZDatabase>"""
REC = b"""<?xml version="1.0"?><QRZDatabase version="1.34" xmlns="http://xmldata.qrz.com"><Callsign>
<call>EA8AB</call><fname>Juan</fname><name>Perez</name><addr2>Las Palmas</addr2><country>Canary Islands</country>
<grid>IL28fc</grid><email>juan@example.org</email></Callsign><Session><Key>abc123</Key></Session></QRZDatabase>"""
EXPIRED = b"""<?xml version="1.0"?><QRZDatabase><Session><Error>Session Timeout</Error></Session></QRZDatabase>"""
NOTFOUND = b"""<?xml version="1.0"?><QRZDatabase><Session><Error>Not found: ZZ9ZZ</Error><Key>abc123</Key></Session></QRZDatabase>"""


class Fake:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, params):
        self.calls.append(params)
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_login_lookup_cache_and_offline(tmp_path):
    f = Fake([LOGIN, REC])
    c = qrz.Client(("W9XYZ", "pw"), tmp_path / "c.sqlite", f)
    r = c.lookup("ea8ab")
    assert (r["fname"], r["grid"], r["source"]) == ("Juan", "IL28fc", "QRZ.com")
    assert f.calls[0]["agent"] == qrz.AGENT and f.calls[1] == {"s": "abc123", "callsign": "EA8AB"}
    assert c.lookup("EA8AB")["fname"] == "Juan" and len(f.calls) == 2          # served from cache
    off = qrz.Client(("W9XYZ", "pw"), tmp_path / "c.sqlite", Fake([OSError("no network")]))
    off.db.execute("UPDATE qrz SET fetched = 0")                                 # stale, network down
    assert off.lookup("EA8AB")["name"] == "Perez"


def test_session_timeout_relogs_and_not_found(tmp_path):
    f = Fake([LOGIN, EXPIRED, LOGIN, REC, NOTFOUND])
    c = qrz.Client(("W9XYZ", "pw"), tmp_path / "c.sqlite", f)
    assert c.lookup("EA8AB")["call"] == "EA8AB" and len(f.calls) == 4
    assert c.lookup("ZZ9ZZ") is None


def test_no_credentials_is_cache_only(tmp_path):
    c = qrz.Client((), tmp_path / "c.sqlite", Fake([]))
    assert c.lookup("EA8AB") is None


def test_cache_readable_from_another_thread(tmp_path):
    import threading
    c = qrz.Client(("W9XYZ", "pw"), tmp_path / "c.sqlite", Fake([LOGIN, REC]))
    c.lookup("EA8AB")
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.lookup("EA8AB", allow_network=False)))
    t.start(); t.join()
    assert out["r"]["fname"] == "Juan"
