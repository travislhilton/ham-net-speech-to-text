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
