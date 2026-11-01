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
