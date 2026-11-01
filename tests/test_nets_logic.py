from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as N

from transcribe.nets import pick_net

T0 = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)


def net(id, start_min, end_min=None, freq=None):
    return N(id=id, started_utc=T0 + timedelta(minutes=start_min),
             ended_utc=None if end_min is None else T0 + timedelta(minutes=end_min), frequency_mhz=freq)


def at(m):
    return T0 + timedelta(minutes=m)


def test_open_net_takes_transmission():
    assert pick_net([net(1, 0)], at(5)).id == 1


def test_before_start_and_after_end_are_outside():
    n = net(1, 10, 20)
    assert pick_net([n], at(5)) is None
    assert pick_net([n], at(25)) is None
    assert pick_net([n], at(10)).id == 1 and pick_net([n], at(20)).id == 1


def test_frequency_separates_overlapping_nets():
    nets = [net(1, 0, freq=146.94), net(2, 0, freq=145.29)]
    assert pick_net(nets, at(5), 145.29).id == 2
    assert pick_net(nets, at(5), 146.9404).id == 1   # within 0.5 kHz
    assert pick_net(nets, at(5), 147.00) is None


def test_exact_frequency_beats_catch_all_even_if_older():
    nets = [net(1, 0, freq=146.94), net(2, 3)]
    assert pick_net(nets, at(5), 146.94).id == 1
    assert pick_net(nets, at(5), 145.29).id == 2


def test_unknown_frequency_goes_to_latest_open_net():
    nets = [net(1, 0, freq=146.94), net(2, 3, freq=145.29)]
    assert pick_net(nets, at(5), None).id == 2


def test_decimal_frequency_from_postgres():
    from decimal import Decimal
    assert pick_net([net(1, 0, freq=Decimal("146.9400"))], at(1), 146.94).id == 1
