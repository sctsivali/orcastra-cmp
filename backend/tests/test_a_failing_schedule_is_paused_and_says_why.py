"""A schedule that stops has to say so, in a place an operator will look.

The pause threshold is five, not the webhook dispatcher's twenty. Twenty is right there because it
retries a ten-second POST and twenty failures is under an hour. A schedule's tick is hours, so
twenty would be weeks of silence: five is about thirty hours on every_6_hours and five days on
daily, both still inside the window where an operator recognises the instance.

`BlockingPoolFull` deliberately counts for nothing. A shed call is this service being busy, not the
cluster being unhealthy, and letting a busy console pause an operator's backups would be a denial
of service we did to ourselves.
"""

from datetime import datetime, timezone

from app.services.snapshots.schedule_authority import Refusal
from app.services.snapshots.schedule_runner import RunOutcome
from app.services.snapshots.schedule_scheduler import PAUSE_AFTER_FAILURES, settle_row
from tests._schedule_fakes import FakeSchedule

UTC = timezone.utc
NOW = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)


def _fail(schedule, error="Cluster 'nuc2' could not be reached."):
    settle_row(schedule, RunOutcome(result="failed", error=error), NOW)


def test_the_last_failure_before_the_threshold_does_not_pause():
    schedule = FakeSchedule()
    for _ in range(PAUSE_AFTER_FAILURES - 1):
        _fail(schedule)
    assert schedule.state == "active"
    assert schedule.consecutive_failures == PAUSE_AFTER_FAILURES - 1
    assert schedule.paused_reason is None


def test_the_threshold_failure_pauses_and_names_the_cluster():
    schedule = FakeSchedule()
    for _ in range(PAUSE_AFTER_FAILURES):
        _fail(schedule)
    assert schedule.state == "paused"
    assert "nuc2" in schedule.paused_reason
    assert schedule.due_slot is None


def test_a_terminal_refusal_pauses_immediately_without_waiting_for_five():
    """An upstream schedule appearing is not a transient fault. Retrying it four more times would
    be four more refusals and a day of an operator not being told."""
    schedule = FakeSchedule()
    settle_row(
        schedule,
        RunOutcome(
            result="refused",
            terminal=True,
            refusal=Refusal(
                code="upstream_schedule_appeared",
                message="ct-x is now snapshotted by the metered product outside this console.",
                terminal=True,
            ),
        ),
        NOW,
    )
    assert schedule.state == "paused"
    assert schedule.consecutive_failures == 1
    assert "metered product" in schedule.paused_reason


def test_a_shed_call_is_not_a_failure():
    schedule = FakeSchedule()
    for _ in range(PAUSE_AFTER_FAILURES * 2):
        settle_row(schedule, RunOutcome(result="deferred", error="the schedule pool is saturated"), NOW)
    assert schedule.state == "active"
    assert schedule.consecutive_failures == 0
    assert schedule.last_error is None


def test_a_shed_call_does_not_move_the_next_run_either():
    """It is our own load, so the schedule is simply tried again on the next tick."""
    schedule = FakeSchedule(due_slot=NOW)
    before = schedule.next_run_at
    settle_row(schedule, RunOutcome(result="deferred"), NOW)
    assert schedule.next_run_at == before
    assert schedule.due_slot == NOW, "a shed call moved the slot it never attempted"
    assert schedule.run_state == "idle", "a shed row stayed claimed and would need reaping"


def test_a_success_clears_the_counter_so_a_flaky_cluster_never_accumulates_to_a_pause():
    schedule = FakeSchedule(due_slot=NOW)
    for _ in range(PAUSE_AFTER_FAILURES - 1):
        _fail(schedule)
    schedule.due_slot = NOW
    settle_row(schedule, RunOutcome(result="success", snapshot="ops-sch12-nightly-x"), NOW)
    assert schedule.consecutive_failures == 0
    assert schedule.last_error is None
    assert schedule.state == "active"


def test_backoff_moves_the_retry_but_never_the_slot():
    schedule = FakeSchedule(due_slot=NOW)
    _fail(schedule)
    assert schedule.due_slot == NOW, "backoff renamed the retry"
    assert schedule.next_run_at > NOW
