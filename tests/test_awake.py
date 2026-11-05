from station.awake import KeepAwake, wanted


def test_held_while_a_radio_listens():
    want, why = wanted("listening", [{"id": "a", "paused": True}, {"id": "b", "name": "FTM", "paused": False}], [])
    assert want and "FTM" in why


def test_released_when_every_radio_is_paused_and_nothing_queued():
    assert wanted("listening", [{"id": "a", "paused": True}], [{"state": "done"}]) == (False, "no radio listening")


def test_held_while_the_accurate_pass_has_work():
    assert wanted("listening", [{"id": "a", "paused": True}], [{"state": "running"}])[0]
    assert wanted("listening", [], [{"state": "queued"}])[0]


def test_modes():
    assert wanted("always", [], [])[0] is True
    assert wanted("off", [{"id": "a", "paused": False}], [{"state": "running"}])[0] is False


def test_keep_awake_is_a_noop_off_windows(monkeypatch):
    k = KeepAwake()
    k.ok = False
    assert k.set(True) is False and k.held is False
