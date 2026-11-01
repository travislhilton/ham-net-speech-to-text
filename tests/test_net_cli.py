"""tools.net against a fake API (no server, no DB)."""
import pytest

from tools import net


class FakeApi:
    def __init__(self, open_nets=()):
        self.open = list(open_nets)
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/nets?open=true":
            return self.open
        if path == "/nets":
            return self.open + [{"id": 1, "name": "Old", "started_utc": "2026-10-01T02:00:00Z",
                                 "ended_utc": "2026-10-01T03:00:00Z"}]
        if path == "/nets/start":
            return {"id": 9, "started_utc": "2026-11-02T03:00:00Z", "ended_utc": None, **body}
        if path.startswith("/nets/") and path.endswith("/stop"):
            nid = int(path.split("/")[2])
            return {"id": nid, "name": "X", "started_utc": "2026-11-02T03:00:00Z",
                    "ended_utc": "2026-11-02T04:00:00Z", "backfilled": 2}
        raise AssertionError(path)


def _net(i, name="Net"):
    return {"id": i, "name": name, "started_utc": "2026-11-02T03:00:00Z", "ended_utc": None}


def test_start_sends_only_given_fields():
    api, lines = FakeApi(), []
    assert net.run(["start", "Springfield Valley ARES", "--freq", "146.94", "--ncs", "W9XYZ"], api, lines.append) == 0
    assert api.calls[-1] == ("POST", "/nets/start",
                             {"name": "Springfield Valley ARES", "frequency_mhz": 146.94, "net_control": "W9XYZ"})
    assert "#9 Springfield Valley ARES 146.94 MHz NCS W9XYZ" in lines[0]
    assert lines[-1].endswith("stop 9")


def test_start_warns_about_an_already_open_net():
    api, lines = FakeApi([_net(4, "Other")]), []
    net.run(["start", "ARES"], api, lines.append)
    assert any("also open" in l and "#4 Other" in l for l in lines)


def test_stop_defaults_to_the_one_open_net():
    api, lines = FakeApi([_net(5)]), []
    assert net.run(["stop"], api, lines.append) == 0
    assert api.calls[-1] == ("POST", "/nets/5/stop", {})
    assert "back-filled 2" in lines[0] and "--net-id 5" in lines[1]


def test_stop_refuses_to_guess_between_open_nets():
    with pytest.raises(net.CliError, match="several nets"):
        net.run(["stop"], FakeApi([_net(5), _net(6)]), print)


def test_stop_with_nothing_open():
    with pytest.raises(net.CliError, match="no open net"):
        net.run(["stop"], FakeApi(), print)


def test_stop_by_id_with_end_time():
    api = FakeApi()
    net.run(["stop", "7", "--at", "2026-11-02T04:00:00Z"], api, lambda *_: None)
    assert api.calls == [("POST", "/nets/7/stop", {"ended_utc": "2026-11-02T04:00:00Z"})]


def test_status_lists_open_or_all():
    lines = []
    net.run(["status"], FakeApi(), lines.append)
    assert lines == ["no open nets"]
    lines.clear()
    net.run(["status", "--all"], FakeApi([_net(3)]), lines.append)
    assert len(lines) == 2 and lines[1].startswith("#1 Old") and "ended" in lines[1]


def test_key_from_env_then_env_file(tmp_path):
    f = tmp_path / "env"
    f.write_text("FOO=1\nexport EMCOMM_STT_KEY='abc'\n")
    assert net.api_key({}, f) == "abc"
    assert net.api_key({"EMCOMM_STT_KEY": "zz"}, f) == "zz"
    assert net.api_key({}, tmp_path / "missing") is None
