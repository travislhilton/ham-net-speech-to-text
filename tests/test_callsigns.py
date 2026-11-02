from transcribe.callsigns import normalize


def test_phonetic_run_becomes_callsign():
    assert normalize("this is kilo seven alpha bravo charlie checking in") == \
        ("this is K7ABC checking in", ["K7ABC"])


def test_whisper_digits_and_commas():
    text, found = normalize("Kilo 7, Alpha Bravo Charlie, no traffic.")
    assert found == ["K7ABC"] and text.startswith("K7ABC")


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


def test_unvalidated_shapes_are_left_alone():
    text, found = normalize("I have 2 A4 sheets and the K9 unit", validate=lambda c: False)
    assert found == [] and text == "I have 2 A4 sheets and the K9 unit"
