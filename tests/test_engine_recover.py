"""Owner 2026-11-07: while a dashboard is open, keep the PC awake and check the live engine (a laptop that slept in
Modern Standby came back writing "The." for every transmission until the engine was reloaded)."""
import copy
import time

import numpy as np

from station import awake
from station.core import SR, Station
from tests.test_station import CFG

GOOD = "Radio check, I can hear you five by five."


class Eng:
    made = 0

    def __init__(self):
        Eng.made += 1
        self.name, self.broken = f"fake-{Eng.made}", False

    def transcribe(self, audio, prompt=None):
        return {"text": "The." if self.broken else GOOD}


def station(tmp_path):
    Eng.made = 0
    return Station(copy.deepcopy(CFG), Eng, data_dir=tmp_path)


def test_a_dashboard_holds_the_pc_awake_while_a_radio_listens():
    r = [{"id": "net"}]
    assert awake.wanted("traffic", r, [], viewer=True) == (True, "dashboard open")
    assert awake.wanted("traffic", r, [])[0] is False
    assert awake.wanted("traffic", [{"id": "net", "paused": True}], [], viewer=True)[0] is False   # no radio listening
    assert awake.wanted("off", r, [], viewer=True)[0] is False                                      # off stays off


def test_dashboard_open_counts_browsers_with_a_grace(tmp_path):
    st = station(tmp_path)
    assert not st.dashboard_open()
    st.viewer(True); st.viewer(True); st.viewer(False)
    assert st.dashboard_open() and st._engine_check
    st.viewer(False)
    assert st.dashboard_open()                                        # a page reload is not "closed"
    st.viewer_seen = time.time() - st.VIEWER_GRACE_S - 1
    assert not st.dashboard_open()


def test_a_broken_engine_is_reloaded_and_the_transmission_read_again(tmp_path):
    st = station(tmp_path); st.viewer(True)
    eng, audio = Eng(), np.zeros(5 * SR, np.float32)
    eng, res = st._maybe_recover(eng, audio, eng.transcribe(audio), "p")       # a good read: the reference
    assert res["text"] == GOOD and st._canary
    eng.broken = True
    new, res = st._maybe_recover(eng, audio, eng.transcribe(audio), "p")
    assert new is not eng and res["text"] == GOOD and st._partial_reload
    assert st.engines["final"] == new.name


def test_no_reload_without_a_dashboard_for_real_noise_or_too_often(tmp_path):
    st = station(tmp_path)
    eng, audio = Eng(), np.zeros(5 * SR, np.float32)
    st._maybe_recover(eng, audio, eng.transcribe(audio), "p")
    eng.broken = True
    assert st._maybe_recover(eng, audio, {"text": "The."}, "p")[0] is eng            # nobody watching
    st.viewer(True); eng.broken = False
    assert st._maybe_recover(eng, audio, {"text": "......"}, "p")[0] is eng          # noise: the engine passes the check
    eng.broken = True; st._engine_reload_at = time.time()
    assert st._maybe_recover(eng, audio, {"text": "The."}, "p")[0] is eng            # reloaded under 5 minutes ago
