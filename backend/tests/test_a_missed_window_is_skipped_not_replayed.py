"""A backend that was down for a day takes ONE snapshot, not a day's worth.

Every catch-up snapshot would capture the same state, the current one, under the timestamp of a
slot it did not capture. Replaying the gap would only fill the retention cap with restore points
that do not exist. The gap is recorded instead, in `missed_runs` and the `.missed` audit.
"""

from datetime import datetime, timedelta, timezone

from app.services.snapshots import recurrence
from app.services.snapshots.schedule_runner import RunOutcome
from app.services.snapshots.schedule_scheduler import choose_slot, settle_row
from tests._schedule_fakes import FakeSchedule

UTC = timezone.utc


def test_a_three_day_outage_produces_one_slot_named_for_today():
    schedule = FakeSchedule(last_run_slot=datetime(2026, 9, 6, 3, 0, tzinfo=UTC))
    back_up = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)
    slot = choose_slot(schedule, back_up)
    assert slot == datetime(2026, 9, 9, 3, 0, tzinfo=UTC), "the run was named for a stale slot"


def test_the_gap_is_counted_rather_than_replayed():
    schedule = FakeSchedule(last_run_slot=datetime(2026, 9, 6, 3, 0, tzinfo=UTC))
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)
    schedule.due_slot = choose_slot(schedule, now)
    settle_row(schedule, RunOutcome(result="success", snapshot="ops-sch12-nightly-x"), now)

    # 7th, 8th and 9th were due; the 9th was taken, so two were lost.
    assert schedule.missed_runs == 2
    assert schedule.last_run_slot == datetime(2026, 9, 9, 3, 0, tzinfo=UTC)
    assert schedule.next_run_at == datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def test_an_uninterrupted_schedule_counts_nothing_missed():
    """The control. Without it a counter that increments on every run would still pass."""
    schedule = FakeSchedule(last_run_slot=datetime(2026, 9, 8, 3, 0, tzinfo=UTC))
    now = datetime(2026, 9, 9, 3, 0, 20, tzinfo=UTC)
    schedule.due_slot = choose_slot(schedule, now)
    settle_row(schedule, RunOutcome(result="success", snapshot="ops-sch12-nightly-x"), now)
    assert schedule.missed_runs == 0


def test_a_schedule_that_has_never_run_counts_nothing_missed():
    """A schedule created last week did not "miss" the slots before it existed."""
    schedule = FakeSchedule(last_run_slot=None)
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)
    schedule.due_slot = choose_slot(schedule, now)
    settle_row(schedule, RunOutcome(result="success", snapshot="ops-sch12-nightly-x"), now)
    assert schedule.missed_runs == 0


def test_the_next_run_is_anchored_rather_than_chained_to_the_finish():
    """A run that finished at 03:00:40 does not move tomorrow's slot to 03:00:40."""
    schedule = FakeSchedule(due_slot=datetime(2026, 9, 9, 3, 0, tzinfo=UTC))
    settle_row(
        schedule,
        RunOutcome(result="success", snapshot="ops-sch12-nightly-x"),
        datetime(2026, 9, 9, 3, 0, 40, tzinfo=UTC),
    )
    assert schedule.next_run_at == datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def test_a_six_hourly_schedule_measures_its_own_gap_in_its_own_periods():
    schedule = FakeSchedule(
        frequency="every_6_hours", hour_utc=2, last_run_slot=datetime(2026, 9, 8, 2, 0, tzinfo=UTC)
    )
    now = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)
    schedule.due_slot = choose_slot(schedule, now)
    settle_row(schedule, RunOutcome(result="success", snapshot="x"), now)
    expected = recurrence.slots_between(
        "every_6_hours", 2, 0, None,
        datetime(2026, 9, 8, 2, 0, tzinfo=UTC), schedule.last_run_slot,
    ) - 1
    assert schedule.missed_runs == expected == 3
