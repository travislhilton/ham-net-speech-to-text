from transcribe.callsigns import normalize


def test_phonetic_run_becomes_callsign():
    assert normalize("this is kilo seven alpha bravo charlie checking in") == \
        ("this is K7ABC checking in", ["K7ABC"])


def test_whisper_digits_and_commas():
    text, found = normalize("Kilo 7, Alpha Bravo Charlie, no traffic.")
    assert found == ["K7ABC"] and text.startswith("K7ABC")


def test_x_ray_with_a_hyphen():
    # Whisper writes "X-ray"; the hyphen split it into "x" + "ray" and normalize raised KeyError
    assert normalize("Kilo Seven X-ray Bravo Charlie, no traffic.")[1] == ["K7XBC"]


def test_written_prefix_then_spelled_suffix():
    # large-v3 with context writes the prefix and spells the rest (synthetic net benchmark, 11-03)
    assert normalize("Next is WA7 X-ray Hotel Echo.") == ("Next is WA7XHE.", ["WA7XHE"])
    assert normalize("this is K-Zero X-ray Echo Tango")[1] == ["K0XET"]
    assert normalize("K0 X-ray Echo Tango, thanks")[1] == ["K0XET"]
    assert normalize("on W7 Echo tonight")[1] == []           # one letter: only when a callbook confirms it
    assert normalize("on W7 Echo tonight", validate=lambda c: c == "W7E")[1] == ["W7E"]


def test_non_callsign_run_left_alone():
    text, found = normalize("alpha bravo charlie delta echo")
    assert found == [] and text == "alpha bravo charlie delta echo"


def test_roster_picks_up_written_callsigns():
    _, found = normalize("W9XYZ here, also heard N7XYZ", roster={"W9XYZ"})
    assert found == ["W9XYZ"]


def test_split_and_international_calls_with_a_validator():
    known = {"W9XYZ", "EA8AB", "VE7CC", "K7ABC"}
    text, found = normalize("This is W9-XYZ, heard EA8AB and V E 7 C C, also W-9-T-L-H again; 146.94 MHz",
                            validate=known.__contains__)
    assert found == ["W9XYZ", "EA8AB", "VE7CC"]
    assert "W9XYZ, heard EA8AB and VE7CC, also W9XYZ again" in text and "146.94" in text


def test_prefix_and_suffix_written_apart_with_a_comma():
    known = {"KK7XKG", "KK7XKE"}.__contains__
    assert normalize("OK, KK7, XKG, Eli, Shelbyville.", validate=known) == ("OK, KK7XKG, Eli, Shelbyville.", ["KK7XKG"])
    assert normalize("KK-7, XKE, will", validate=known)[1] == ["KK7XKE"]
    assert normalize("KB7, XLG Marvin", validate=known) == ("KB7, XLG Marvin", [])   # unknown: left as heard


def test_unvalidated_shapes_are_left_alone():
    text, found = normalize("I have 2 A4 sheets and the K9 unit", validate=lambda c: False)
    assert found == [] and text == "I have 2 A4 sheets and the K9 unit"
