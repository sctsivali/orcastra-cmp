"""When a schedule fires, and why the vocabulary is four frequencies instead of a cron string.

A cron field would need a parser, a dependency and a syntax operators have to learn. Four
frequencies cover what an operator console is asked for, and a Select cannot produce an invalid
value. It can still produce a combination of fields that means nothing, which most of these tests
cover.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.snapshots import recurrence as r

UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


# ── the vocabulary is closed ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("frequency", list(r.FREQUENCIES))
def test_every_named_frequency_has_a_period(frequency):
    assert r.period_hours(frequency) > 0


def test_an_unknown_frequency_is_refused_by_name():
    with pytest.raises(r.RecurrenceError) as caught:
        r.period("hourly")
    assert "hourly" in str(caught.value)


@pytest.mark.parametrize("minute", [1, 7, 30, 59])
def test_only_quarter_hours_are_accepted(minute):
    if minute in r.MINUTES:
        r.validate("daily", 3, minute, None)
        return
    with pytest.raises(r.RecurrenceError):
        r.validate("daily", 3, minute, None)


def test_a_weekly_schedule_needs_a_weekday_and_nothing_else_may_have_one():
    with pytest.raises(r.RecurrenceError, match="needs a weekday"):
        r.validate("weekly", 3, 0, None)
    with pytest.raises(r.RecurrenceError, match="Only a weekly"):
        r.validate("daily", 3, 0, 2)
    r.validate("weekly", 3, 0, 2)


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_an_hour_outside_the_day_is_refused(hour):
    with pytest.raises(r.RecurrenceError):
        r.validate("daily", hour, 0, None)


# ── slots are anchored, not chained ────────────────────────────────────────────────────────────

def test_a_daily_slot_lands_on_its_own_hour_every_day():
    for day in range(1, 15):
        slot = r.next_slot_after("daily", 3, 30, None, at(2026, 9, day, 0, 0))
        assert (slot.hour, slot.minute, slot.second) == (3, 30, 0)
        assert slot.day == day


def test_a_six_hourly_schedule_keeps_the_phase_of_its_anchor_hour():
    """02:00 means 02, 08, 14, 20, not "six hours after whenever it was created"."""
    hours = set()
    moment = at(2026, 9, 9, 0, 0)
    for _ in range(8):
        moment = r.next_slot_after("every_6_hours", 2, 0, None, moment)
        hours.add(moment.hour)
    assert hours == {2, 8, 14, 20}


def test_a_weekly_schedule_lands_on_its_weekday():
    for weekday in range(7):
        slot = r.next_slot_after("weekly", 5, 15, weekday, at(2026, 9, 9, 0, 0))
        assert slot.weekday() == weekday, f"weekday {weekday} landed on {slot}"
        assert (slot.hour, slot.minute) == (5, 15)


def test_the_next_slot_is_strictly_later_than_a_slot_itself():
    """Otherwise a schedule that has just fired claims the same slot again, forever."""
    slot = r.latest_slot_at_or_before("daily", 3, 0, None, at(2026, 9, 9, 12, 0))
    assert r.next_slot_after("daily", 3, 0, None, slot) > slot


def test_a_run_that_took_time_does_not_push_every_future_run_later():
    """The next slot follows the slot itself. Chaining from `finished_at` would push the schedule
    later with every run until a nightly snapshot lands in the working day."""
    slot = r.latest_slot_at_or_before("daily", 3, 0, None, at(2026, 9, 9, 3, 0, 40))
    assert slot == at(2026, 9, 9, 3, 0)
    assert r.next_slot_after("daily", 3, 0, None, slot) == at(2026, 9, 10, 3, 0)


def test_utc_arithmetic_survives_a_year_with_no_transition_artefact():
    """No local time anywhere, so no slot is skipped or doubled on a daylight-saving boundary."""
    moment = at(2026, 1, 1, 0, 0)
    seen = 0
    while moment < at(2027, 1, 1, 0, 0):
        nxt = r.next_slot_after("daily", 2, 30, None, moment)
        assert (nxt.hour, nxt.minute) == (2, 30)
        assert timedelta(0) < nxt - moment <= timedelta(days=1)
        moment = nxt
        if nxt.year == 2026:
            seen += 1
    # 2026 is not a leap year, and every day of it gets exactly one slot.
    assert seen == 365


# ── counting what an outage cost ───────────────────────────────────────────────────────────────

def test_slots_between_counts_the_gap_a_backend_outage_left():
    a = r.latest_slot_at_or_before("daily", 3, 0, None, at(2026, 9, 6, 4, 0))
    b = r.latest_slot_at_or_before("daily", 3, 0, None, at(2026, 9, 9, 4, 0))
    assert r.slots_between("daily", 3, 0, None, a, b) == 3


def test_a_naive_datetime_is_read_as_utc_rather_than_refused():
    """A value that has been through a serialisation boundary may arrive naive. Refusing it would
    turn a formatting detail into a schedule that stops."""
    aware = r.latest_slot_at_or_before("daily", 3, 0, None, at(2026, 9, 9, 12, 0))
    naive = r.latest_slot_at_or_before("daily", 3, 0, None, datetime(2026, 9, 9, 12, 0))
    assert aware == naive


def test_the_summary_names_the_time_it_actually_fires():
    assert r.describe("daily", 3, 30, None) == "daily at 03:30 UTC"
    assert r.describe("weekly", 20, 0, 0) == "every Monday at 20:00 UTC"
    assert "6 hours" in r.describe("every_6_hours", 2, 0, None)
