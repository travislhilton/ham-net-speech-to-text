"""Frequencies typed for a radio are remembered (newest first, no repeats, at most 12) for the Change box's quick picks,
and survive a restart (data/operator.json)."""
import copy

from station import core, engine
from station.core import Station
from tests.test_agent import _station
from tests.test_cat import CFG


def test_typed_frequencies_are_remembered_newest_first_without_repeats(tmp_path):
    st = _station(tmp_path)
    for f, m in ((146.94, "FM"), (3.915, "LSB"), (146.94, "FM"), (7.2, "LSB")):
        st.set_radio("ic", f, m)
    assert [(x["f"], x["m"]) for x in st.radios["ic"]["recent_freqs"]] == [(7.2, "LSB"), (146.94, "FM"), (3.915, "LSB")]
    for i in range(20):
        st.set_radio("ic", 144.1 + i / 100, "FM")
    assert len(st.radios["ic"]["recent_freqs"]) == core.RECENT_FREQS


def test_they_survive_a_restart(tmp_path):
    st = _station(tmp_path)
    st.set_radio("ic", 146.52, "FM")
    st.set_radio("ic", 3.915, "LSB")
    again = Station(copy.deepcopy(CFG), lambda: engine.FakeEngine(), data_dir=tmp_path)
    assert [x["f"] for x in again.radios["ic"]["recent_freqs"]] == [3.915, 146.52]
