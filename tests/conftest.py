"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_power_changes(monkeypatch):
    """Tests start real Stations. On a Windows station PC their KeepAwake would switch the PC's actual power plan
    (and, with each test's snapshot in a temp folder, never put it back: seen on both station PCs, 2026-11-05)."""
    from station import awake
    orig = awake.KeepAwake.__init__

    def init(self, *a, **k):
        orig(self, *a, **{**k, "snapshot": None})
        self.ok = False
    monkeypatch.setattr(awake.KeepAwake, "__init__", init)


@pytest.fixture(autouse=True)
def _no_real_recording_levels(monkeypatch):
    """Set level… reads and sets the Windows recording level of a radio's input by name; the test radios are named
    like real ones (NET-REC), so on a station PC a test would find, and could change, the real input (2026-11-05)."""
    from station import levels
    monkeypatch.setattr(levels.WinGain, "_find", lambda self, name: None)
