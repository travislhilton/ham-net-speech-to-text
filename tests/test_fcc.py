"""hamkit.fcc: weekly load, daily delta, best-license choice, call parsing (synthetic ULS data)."""
import io, zipfile

from hamkit import fcc


def _zip(**files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, lines in files.items():
            z.writestr(f"{name}.dat", "\r\n".join(lines) + "\r\n")
    return b.getvalue()


WEEKLY = _zip(
    HD=["HD|100|||K7ABC|C|HA|01/02/2001|01/02/2011|01/03/2011|",          # old cancelled license
        "HD|200|||K7ABC|A|HA|05/06/2020|05/06/2030||",
        "HD|300|||W9XYZ|A|HV|10/19/2021|10/19/2031||"],
    EN=["EN|100|||K7ABC|L|L1|Old, Name|Old||Name|||||1 Old St|Springfield|IL|62701|||000|0000000001|I|",
        "EN|200|||K7ABC|L|L2|Doe, Jane Q|Jane|Q|Doe||||j@example.org|2 Main St|Springfield|IL|62701|||000|0000000002|I|",
        "EN|300|||W9XYZ|L|L3|Sample, Pat Q|Pat|Q|Sample|||||1 Test Rd|Springfield|IL|62701|||000|0000000003|I|"],
    AM=["AM|200|||K7ABC|E|C|7||||||E||K7OLD|T|", "AM|300|||W9XYZ|G|C|7||||||E||KI7AAA|T|"])
DAILY = _zip(HD=["HD|300|||W9XYZ|A|HV|10/19/2021|10/19/2031||"],
             EN=["EN|300|||W9XYZ|L|L3|Sample, Pat Q|Pat|Q|Sample|||||1 Test Rd|Springfield|IL|62701|||000|0000000003|I|"],
             AM=["AM|300|||W9XYZ|E|C|7||||||E||KI7AAA|G|"])


def test_weekly_then_daily(tmp_path):
    con = fcc.connect(tmp_path / "t.sqlite")
    assert fcc.load_zip(con, WEEKLY) == {"HD": 3, "EN": 3, "AM": 2}
    k = fcc.lookup(con, "k7abc/p")
    assert (k["usi"], k["name"], k["class_name"], k["status_name"], k["email"]) == \
           (200, "Jane Q Doe", "Amateur Extra", "Active", "j@example.org")
    assert k["grant_date"] == "2020-05-06" and k["valid"] and k["previous_call"] == "K7OLD"
    w = fcc.lookup(con, "W9XYZ")
    assert (w["class_name"], w["city"]) == ("General", "Springfield")
    fcc.load_zip(con, DAILY)                                    # upgrade + move
    w = fcc.lookup(con, "W9XYZ")
    assert (w["class_name"], w["city"]) == ("Amateur Extra", "Springfield")
    assert fcc.lookup(con, "N0CALL") is None


def test_embedded_newline_continues_the_record():
    raw = b"EN|1|||K1A|L|L|Name|A||B||||e|street\r\nline two|City|IL|1|||000|9|I|\r\nEN|2|||K1B|L|"
    recs = list(fcc._records(raw, "EN"))
    assert len(recs) == 2 and recs[0][15] == "street line two" and recs[0][16] == "City"


def test_base_call():
    assert fcc.base_call("VE3/K7ABC") == "K7ABC"
    assert fcc.base_call("K7ABC/MM") == "K7ABC"
    assert fcc.base_call("W9XYZ-5") == "W9XYZ-5"
