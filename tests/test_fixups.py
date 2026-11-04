"""Local-name fix-ups after transcription (station/fixups.py): whole words, case kept, context-gated, the net's own
name repaired by sound only when it clearly is that name."""
from pathlib import Path

from station import fixups

ROOT = Path(__file__).resolve().parent.parent
NAME = "Springfield County ARES net"


def cfg():
    return fixups.load(ROOT / "config", None)


def fix(s, names=(NAME,)):
    return fixups.apply(s, cfg(), names)


def test_generic_rules_in_radio_context_only():
    assert fix("The purpose of this net is to gather Aries members.")[0] == \
        "The purpose of this net is to gather ARES members."
    assert fix("If you have not sent your Windlink message")[0] == "If you have not sent your Winlink message"
    assert fix("WINDLINK check")[0] == "WINLINK check"                  # case kept
    assert fix("Wind, link message")[0] == "Winlink message"            # punctuation between words ignored
    assert fix("Look at the constellation Aries tonight.") == ("Look at the constellation Aries tonight.", [])
    assert fix("Ariesnet and Windlinks stay.")[1] == []                  # whole words only
    t, ch = fix("This is the nest control station.")
    assert t == "This is the net control station." and ch[0]["heard"] == "nest control"


def test_the_nets_own_name_by_sound():
    t, ch = fix("Calling the Springfeld Country Aries net, good evening.")
    assert t == "Calling the Springfield County ARES net, good evening."
    assert ch[-1]["heard"] == "Springfeld Country ARES net" and "sounds" in ch[-1]["why"]
    assert fix("welcome to the Springfield Count Ares Net tonight")[0] == "welcome to the Springfield County ARES Net tonight"
    for s in ("the SARC net is at nine", "the Shelbyville County ARES net", "Good evening, Morgan Internet.",
              "the Springfield ARES net", "This is the Springfield County ARES net."):
        assert fix(s)[0] == s.replace("Aries", "ARES"), s                # another net, a dropped word, already right
    assert fix("Calling the Springfeld Country Aries net", names=())[0] == "Calling the Springfeld Country ARES net"


def test_rules_merge_vocab_local_and_learned(tmp_path):
    (tmp_path / "config").mkdir(); (tmp_path / "data").mkdir()
    (tmp_path / "config" / "vocab.yaml").write_text("fixups:\n  - {heard: Windlink, say: Winlink}\n"
                                                    "  - {heard: Springfeld, say: Springfield}\n")
    (tmp_path / "config" / "local.yaml").write_text("fixups:\n  Shelbyvile: Shelbyville\nnet_names: [Ogdenville ARES net]\n")
    # the dashboard's corrections: lower case, punctuation stripped, with counts the reader ignores
    (tmp_path / "data" / "fixups_learned.yaml").write_text(
        "fixups:\n  - {heard: springfeld, say: Springfield West, count: 3, first_at: '2026-11-03T20:01:00'}\n"
        "  - {heard: morgan internet, say: and net, count: 1}\n")
    c = fixups.load(tmp_path / "config", tmp_path / "data")
    assert {r["heard"].lower(): r["source"] for r in c["rules"]} == {
        "windlink": "vocab", "springfeld": "learned", "shelbyvile": "local", "morgan internet": "learned"}
    assert c["net_names"] == ["Ogdenville ARES net"]
    assert fixups.apply("springfeld and Shelbyvile", c)[0] == "Springfield West and Shelbyville"
    assert fixups.apply("Good evening, Morgan Internet.", c)[0] == "Good evening, And net."
    assert fixups.apply("calling the Ogdenvile Aries net", c)[0] == "calling the Ogdenville ARES net"
    assert fixups.load(tmp_path / "nowhere", None) == {"rules": [], "net_names": []}


def test_net_names_from_the_schedule():
    assert fixups.net_names_for({"name": "SCARES", "spoken": ["Springfield County ARES net"]}) == [NAME]
    assert fixups.net_names_for({"name": "Springfield ARES Net"}, ["Other net"]) == ["Springfield ARES Net", "Other net"]
    assert fixups.net_names_for(None) == []
