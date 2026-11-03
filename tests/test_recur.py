"""Outlook-style repetition for scheduled nets (station/recur.py): rules, month ends, 5th weekdays, leap years,
daylight saving, counts, exceptions, and the plain-language summary."""
from datetime import date, datetime

import pytest

from station import recur


def days(rule, a, b):
    return [d.isoformat() for d in recur.dates(recur.normalize(rule), date.fromisoformat(a), date.fromisoformat(b))]


def test_legacy_days_become_weekly():
    assert recur.normalize(None, ["Thu", "tue"]) == {"freq": "weekly", "weekdays": ["Tue", "Thu"]}
    assert recur.normalize(None, None) == {"freq": "weekly", "weekdays": recur.DAYS}
    assert days(None, "2026-11-01", "2026-11-07") == [f"2026-11-0{i}" for i in range(1, 8)]


def test_daily_weekly_and_intervals():
    assert days({"freq": "daily", "interval": 3, "starts": "2026-11-01"}, "2026-11-01", "2026-11-10") == \
        ["2026-11-01", "2026-11-04", "2026-11-07", "2026-11-10"]
    assert days({"freq": "weekly", "weekdays": ["Tue", "Thu"]}, "2026-11-01", "2026-11-08") == ["2026-11-03", "2026-11-05"]
    # every 2 weeks, counted from the week of the start date (a Wednesday start still meets that Tuesday's week)
    assert days({"freq": "weekly", "interval": 2, "weekdays": ["Tue"], "starts": "2026-11-04"}, "2026-11-01",
                "2026-12-01") == ["2026-11-17", "2026-12-01"]
    with pytest.raises(ValueError):
        recur.normalize({"freq": "weekly", "interval": 2, "weekdays": ["Tue"]})          # needs an anchor


def test_monthly_by_day_month_ends_and_leap_years():
    r = {"freq": "monthly", "monthday": 31}
    assert days(r, "2027-01-01", "2027-05-31") == ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31"]
    assert days(r, "2028-02-01", "2028-02-29") == ["2028-02-29"]                         # leap year
    assert days({"freq": "monthly", "monthday": "last"}, "2026-02-01", "2026-04-30") == \
        ["2026-02-28", "2026-03-31", "2026-04-30"]
    assert days({"freq": "monthly", "monthday": 15, "interval": 3, "starts": "2026-01-15"}, "2026-01-01",
                "2026-12-31") == ["2026-01-15", "2026-04-15", "2026-07-15", "2026-10-15"]


def test_monthly_nth_weekday():
    first_third_sun = {"freq": "monthly", "nth": [1, 3], "weekdays": ["Sun"]}
    assert days(first_third_sun, "2026-11-01", "2026-12-31") == ["2026-11-01", "2026-11-15", "2026-12-06", "2026-12-20"]
    # the 5th Thursday exists only in some months
    assert days({"freq": "monthly", "nth": [5], "weekdays": ["Thu"]}, "2026-01-01", "2026-12-31") == \
        ["2026-01-29", "2026-04-30", "2026-07-30", "2026-10-29", "2026-12-31"]
    assert days({"freq": "monthly", "nth": ["last"], "weekdays": ["Fri"]}, "2026-11-01", "2027-01-31") == \
        ["2026-11-27", "2026-12-25", "2027-01-29"]
    assert recur.normalize({"freq": "monthly", "nth": "1st, last", "weekdays": "sun"})["nth"] == [1, -1]


def test_yearly():
    thanksgiving = {"freq": "yearly", "month": 11, "nth": [4], "weekdays": ["Thu"]}
    assert days(thanksgiving, "2026-01-01", "2028-12-31") == ["2026-11-26", "2027-11-25", "2028-11-23"]
    leap = {"freq": "yearly", "month": 2, "monthday": 29}
    assert days(leap, "2027-01-01", "2028-12-31") == ["2027-02-28", "2028-02-29"]           # Outlook: the last day
    assert recur.normalize({"freq": "yearly", "starts": "2026-06-27"}) == \
        {"freq": "yearly", "month": 6, "monthday": 27, "starts": "2026-06-27"}


def test_start_end_count_and_once():
    r = {"freq": "weekly", "weekdays": ["Tue"], "starts": "2026-11-03", "count": 3}
    assert days(r, "2026-10-01", "2027-01-01") == ["2026-11-03", "2026-11-10", "2026-11-17"]
    assert days(r, "2026-11-12", "2027-01-01") == ["2026-11-17"]                  # counted from the start, not the window
    r = {"freq": "daily", "starts": "2026-11-28", "until": "2026-12-01"}
    assert days(r, "2026-11-01", "2026-12-31") == ["2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01"]
    assert days({"freq": "once", "starts": "2026-12-05"}, "2026-11-01", "2027-01-01") == ["2026-12-05"]
    for bad in ({"freq": "hourly"}, {"freq": "once"}, {"freq": "weekly", "weekdays": []},
                {"freq": "monthly", "nth": [0], "weekdays": ["Sun"]}, {"freq": "monthly", "monthday": 32},
                {"freq": "daily", "starts": "2026-11-05", "until": "2026-11-01"},
                {"freq": "daily", "starts": "2026-11-05", "until": "2026-12-01", "count": 4},
                {"freq": "daily", "count": 4}, {"freq": "daily", "starts": "11/5/2026"}):
        with pytest.raises(ValueError):
            recur.normalize(bad)


def test_daylight_saving_keeps_the_wall_clock_time():
    """The PC's local wall-clock time: a Sunday 19:00 net is at 19:00 on the days the clocks change (US 2026:
    8 March and 1 November), and a 01:30 net on the fall-back day happens once."""
    net = {"id": "n", "start": "19:00", "duration_min": 60, "repeat": {"freq": "weekly", "weekdays": ["Sun"]}}
    occ = recur.occurrences(net, datetime(2026, 3, 1), datetime(2026, 3, 16))
    assert [o["start"] for o in occ] == [datetime(2026, 3, 1, 19), datetime(2026, 3, 8, 19), datetime(2026, 3, 15, 19)]
    occ = recur.occurrences(net | {"start": "01:30"}, datetime(2026, 11, 1), datetime(2026, 11, 2))
    assert [o["start"] for o in occ] == [datetime(2026, 11, 1, 1, 30)]
    assert occ[0]["end"] == datetime(2026, 11, 1, 2, 30)


def test_exceptions_cancel_and_move():
    net = {"id": "n1", "name": "Tuesday Net", "start": "19:00", "duration_min": 60, "freq_mhz": 146.94, "mode": "FM",
           "repeat": {"freq": "weekly", "weekdays": ["Tue"]},
           "exceptions": recur.normalize_exceptions({
               "2026-11-10": {"cancel": True, "note": "Veterans Day"},
               "2026-11-24": {"date": "2026-11-23", "start": "18:30", "freq_mhz": "147,0"}})}
    occ = recur.occurrences(net, datetime(2026, 11, 1), datetime(2026, 12, 1))
    assert [(o["date"], o["start"].isoformat(), o["freq_mhz"], o["moved"]) for o in occ] == [
        ("2026-11-03", "2026-11-03T19:00:00", 146.94, False), ("2026-11-17", "2026-11-17T19:00:00", 146.94, False),
        ("2026-11-24", "2026-11-23T18:30:00", 147.0, True)]
    shown = recur.occurrences(net, datetime(2026, 11, 9), datetime(2026, 11, 11), cancelled=True)
    assert shown[0]["cancelled"] and shown[0]["note"] == "Veterans Day"
    # a move into the window from outside it is still found
    late = recur.occurrences(net | {"exceptions": {"2026-12-01": {"date": "2026-11-28"}}}, datetime(2026, 11, 28),
                             datetime(2026, 11, 29))
    assert [o["date"] for o in late] == ["2026-12-01"] and late[0]["start"] == datetime(2026, 11, 28, 19)
    with pytest.raises(ValueError):
        recur.normalize_exceptions({"2026-11-10": {"mode": "SSB"}})


def test_upcoming_and_summary():
    net = {"id": "n", "start": "19:00", "duration_min": 60, "freq_mhz": 146.94, "mode": "FM",
           "repeat": {"freq": "monthly", "nth": [1, 3], "weekdays": ["Sun"]}}
    up = recur.upcoming(net, datetime(2026, 11, 3, 12), 5)
    assert [o["date"] for o in up] == ["2026-11-15", "2026-12-06", "2026-12-20", "2027-01-03", "2027-01-17"]
    assert recur.upcoming(net, datetime(2026, 11, 15, 19, 30), 1)[0]["date"] == "2026-11-15"      # on the air now
    assert recur.summary(net) == "Every 1st and 3rd Sunday at 19:00, 146.940 MHz FM, 60 min"
    d = lambda r: recur.describe(recur.normalize(r))
    assert d({"freq": "weekly", "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"]}) == "Every weekday"
    assert d({"freq": "weekly", "weekdays": ["Tue", "Thu"], "interval": 2, "starts": "2026-11-03"}) == \
        "Every 2 weeks on Tuesday and Thursday"
    assert d({"freq": "monthly", "monthday": -1}) == "On the last day of every month"
    assert d({"freq": "monthly", "nth": [-1], "weekdays": ["Fri"]}) == "Every last Friday"
    assert d({"freq": "yearly", "month": 6, "nth": [4], "weekdays": ["Sat"]}) == \
        "Every year on the 4th Saturday of June"
    assert d({"freq": "daily"}) == "Every day" and d({"freq": "once", "starts": "2026-12-05"}) == \
        "Once, on Saturday 5 Dec 2026"
    assert recur.summary(net | {"repeat": {"freq": "weekly", "weekdays": ["Tue"], "starts": "2020-01-07",
                                           "count": 10}}).endswith(", 10 times")
    assert recur.ended(net | {"repeat": {"freq": "once", "starts": "2026-01-01"}}, datetime(2026, 11, 3))
