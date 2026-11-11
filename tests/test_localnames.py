"""Local names (station/localnames.py): a club whose name is said as a word ("PARC" said "park") is repaired in the
shapes Whisper really writes it, never in ordinary speech; scoped by net name; shared as an area pack; listed in
the ICS-213's Local terms. (Shapes seen on a real net, 2026-11-10, club renamed: "the Sark Link repeater system", "the Sarkowitz Repair",
"the Sark webpage", "the SarkNet", "SarkOnline"; tested here with a made-up Springfield club, PARC.)"""
import copy
import json
import threading
import urllib.request

import pytest

from station import engine, fixups, ics, localnames as ln
from station.core import Station
from station.server import serve

PARC = {"name": "PARC", "meaning": "Springfield Park Amateur Radio Club", "sounds": ["park"],
        "web": "parconline.org", "also": [{"heard": "Parkowitz Repair", "say": "PARC Link repeater"}]}
SCARES = {"name": "SCARES", "meaning": "Springfield County ARES", "said": "letters",
          "also": ["S C Aries = SCARES"], "nets": ["Springfield County ARES Net"]}


def fix(text, entries=(PARC,), net=None):
    return ln.apply(text, ln.rules([ln.validate(e) for e in entries], net))[0]


@pytest.mark.parametrize("heard, want", [
    ("This net is held each Tuesday evening on the Park Link repeater system.",
     "This net is held each Tuesday evening on the PARC Link repeater system."),
    ("This net is held each Tuesday evening on the Parkowitz Repair",
     "This net is held each Tuesday evening on the PARC Link repeater"),
    ("Go to the Park webpage and open that up.", "Go to the PARC webpage and open that up."),
    ("As announced on the net, the ParkNet, the roll call has a link.",
     "As announced on the net, the PARC Net, the roll call has a link."),
    ("You can go to ParkOnline under nets.", "You can go to parconline.org under nets."),
    ("Look it up at park online dot org tonight.", "Look it up at parconline.org tonight."),
    ("Find information online at parkonline.com, and check it.", "Find information online at parconline.org, and check it."),
    ("Go to PARC online under licensing.", "Go to parconline.org under licensing."),
    ("the park net starts at nine", "the PARC net starts at nine"),
    ("P A R C members, welcome.", "PARC members, welcome."),
])
def test_the_shapes_whisper_writes_are_repaired(heard, want):
    assert fix(heard) == want


@pytest.mark.parametrize("said", ["We met at the park after the net.", "Park the car and check in to the net.",
                                  "The kids are at the park tonight, over."])
def test_ordinary_speech_is_left_alone(said):
    assert fix(said) == said


def test_scoped_to_its_nets_by_name_and_letters_entries():
    assert fix("S C Aries check-ins, please.", (SCARES,), "Springfield County ARES Net") == "SCARES check-ins, please."
    assert fix("S C Aries check-ins, please.", (SCARES,), "springfield county ares net") == "SCARES check-ins, please."
    assert fix("S C Aries check-ins, please.", (SCARES,), "Another Net") == "S C Aries check-ins, please."
    assert fix("S C Aries check-ins, please.", (SCARES,), None) == "S C Aries check-ins, please."
    assert fix("S.C.A.R.E.S. tonight", (SCARES,), "Springfield County ARES Net") == "SCARES tonight"


def test_validation():
    with pytest.raises(ValueError):
        ln.validate({"name": ""})
    with pytest.raises(ValueError):
        ln.validate({"name": "PARC", "web": "not a site"})
    e = ln.validate({"name": "PARC", "web": "https://www.PARCOnline.org/", "sounds": "park, parque"})
    assert e["web"] == "parconline.org" and e["sounds"] == ["park", "parque"] and e["said"] == "word"
    assert ln.validate({"name": "UVX"})["said"] == "letters"


def test_fixups_apply_runs_local_names_first(tmp_path):
    ln.save(tmp_path, [PARC], "Springfield")
    cfg = fixups.load(tmp_path / "nowhere", tmp_path)
    t, ch = fixups.apply("Check in on the ParkNet tonight.", cfg, (), "Any net")
    assert t == "Check in on the PARC Net tonight." and ch[0]["why"] == "local name (PARC)"


def test_an_area_pack_round_trips_and_merges(tmp_path):
    ln.save(tmp_path, [PARC], "Springfield")
    p = ln.pack(ln.load(tmp_path), "Springfield")
    assert p["schema"] == "hamnet-area/1" and p["area"] == "Springfield" and p["local_names"][0]["name"] == "PARC"
    other = tmp_path / "other"
    ln.save(other, [dict(PARC, meaning="old meaning")])
    merged, note = ln.unpack(p, ln.load(other))
    assert note == {"added": 0, "updated": 1, "area": "Springfield"} and merged[0]["meaning"].startswith("Springfield")
    merged, note = ln.unpack({"schema": "hamnet-area/1", "local_names": [SCARES]}, merged)
    assert note["added"] == 1 and [e["name"] for e in merged] == ["PARC", "SCARES"]
    with pytest.raises(ValueError):
        ln.unpack({"schema": "something-else/9"}, [])
    f = tmp_path / "pack.json"
    f.write_text(json.dumps(p), encoding="utf-8")
    fresh = tmp_path / "fresh"
    assert ln.main(["import", str(f), "--data", str(fresh)]) == 0 and ln.load(fresh)[0]["name"] == "PARC"
    assert ln.main(["export", str(tmp_path / "out.json"), "--data", str(fresh)]) == 0
    assert json.loads((tmp_path / "out.json").read_text())["area"] == "Springfield"
    with pytest.raises(ValueError):
        ln.fetch("ftp://example.org/pack.json")


def test_the_ics213_lists_the_local_terms_said_on_the_net():
    terms = ln.footer(ln.terms_in(["Check in on the PARC Net.", "See parconline.org"], [ln.validate(PARC),
                                                                                        ln.validate(SCARES)]))
    assert terms == "Local terms:\n  PARC: Springfield Park Amateur Radio Club (parconline.org)"
    tx = [{"id": "1", "radio": "a", "start": "2026-11-10T20:00:00", "text": "Check in on the PARC Net.",
           "callsigns": [], "status": "final"}]
    d = ics.drafts(tx, {"freq_mhz": 442.1, "mode": "FM"}, {"callsign": "W9XYZ"}, [], None, "Test net", local_terms=terms)
    msg = next(x for x in d if x["form"] == "ics213")["values"]["Message"]
    assert "Local terms:\n  PARC: Springfield Park Amateur Radio Club (parconline.org)" in msg
    assert msg.index("Local terms") < msg.index("The full log is in the attached ICS-309")


def test_settings_api_saves_imports_and_exports(tmp_path):
    cfg = {"callsign": "W9XYZ", "radios": [{"id": "a", "name": "FTM", "device": "x", "gate": "carrier", "freq_mhz": 145.9}],
           "schedule": [{"name": "Springfield County ARES Net", "radio": "a", "freq_mhz": 442.1, "mode": "FM",
                         "days": ["Tue"], "start": "20:00", "duration_min": 60}],
           "port": 0, "bind": "127.0.0.1", "qrz_online": False}
    st = Station(copy.deepcopy(cfg), lambda: engine.FakeEngine(), data_dir=tmp_path / "d")
    srv = serve(st, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        r = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(r).read())
    try:
        d = json.loads(urllib.request.urlopen(base + "/api/local-names").read())
        assert d["names"] == [] and d["nets"] == ["Springfield County ARES Net"]
        d = post("/api/local-names", {"names": [PARC], "area": "Springfield"})
        assert d["names"][0]["web"] == "parconline.org" and d["area"] == "Springfield"
        d = post("/api/local-names/import", {"pack": {"schema": "hamnet-area/1", "local_names": [SCARES]}})
        assert [e["name"] for e in d["names"]] == ["PARC", "SCARES"] and d["imported"]["added"] == 1
        pack = json.loads(urllib.request.urlopen(base + "/api/local-names/pack").read())
        assert pack["area"] == "Springfield" and len(pack["local_names"]) == 2
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/local-names", {"names": [{"name": "PARC", "web": "nope"}]})
        assert e.value.code == 400
    finally:
        srv.shutdown()


def test_the_agreement_pass_repairs_a_net_and_keeps_what_was_heard(tmp_path):
    from station import consensus
    ln.save(tmp_path, [PARC, SCARES])
    cfg = fixups.load(tmp_path / "nowhere", tmp_path)
    rows = [{"id": "1", "radio": "a", "status": "final", "start": "2026-11-10T20:50:49",
             "text": "Just a reminder, as announced on the net, the ParkNet roll call is open.", "callsigns": []},
            {"id": "2", "radio": "a", "status": "final", "start": "2026-11-10T20:51:10",
             "text": "S C Aries members, check in please.", "callsigns": []}]
    recs, _ = consensus.for_session(rows, fix_cfg=cfg, net_name="Springfield County ARES Net")
    v = consensus.apply_view(ics.view(rows[0], agreed=False), recs["1"])
    assert v["text"] == "Just a reminder, as announced on the net, the PARC Net roll call is open."
    assert v["heard_text"].endswith("the ParkNet roll call is open.")
    assert recs["2"]["text"] == "SCARES members, check in please."
    recs, _ = consensus.for_session(rows, fix_cfg=cfg, net_name="Another Net")      # SCARES is not for that net
    assert "2" not in recs and recs["1"]["text"].startswith("Just a reminder")
