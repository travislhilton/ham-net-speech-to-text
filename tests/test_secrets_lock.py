"""The secrets file is locked to this user by SID (an SSH session's WORKGROUP\\user maps to nothing)."""
from types import SimpleNamespace

from station import secrets


def test_the_user_is_named_by_sid_then_by_name_never_left_out(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "op")
    calls = []

    def run(a):
        calls.append(a)
        if a[0] == "whoami":
            return SimpleNamespace(returncode=0, stdout='"w9xyz-pc\\\\op","S-1-5-21-111-222-333-1001"\r\n')
        return SimpleNamespace(returncode=0 if len(calls) > 2 else 1332, stdout="")
    assert secrets._lock(tmp_path / "secrets.env", run=run)
    assert calls[1][-1] == "*S-1-5-21-111-222-333-1001:F" and "/inheritance:r" in calls[1]
    assert calls[2][-1] == "op:F"                       # by SID refused: by name, still with this user


def test_a_lock_that_fails_says_so(tmp_path, capsys):
    run = lambda a: SimpleNamespace(returncode=5, stdout="")   # noqa: E731
    assert not secrets._lock(tmp_path / "secrets.env", run=run)
    assert "could not restrict" in capsys.readouterr().out
