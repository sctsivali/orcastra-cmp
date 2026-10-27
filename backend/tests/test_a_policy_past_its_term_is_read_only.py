"""A tenant policy follows the paid term it stands for: view only after the term, nothing after access ends.

The owner's decision (2026-10-27): once OrcaHub's paid term is over the buyer may look at the
instance and do nothing else, and once the retention window is over the buyer sees nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import tenant_term
from app.core.tenant_term import (
    ACTIVE,
    ENDED,
    ENDING,
    READ_ONLY,
    TermDates,
    TermDatesInvalid,
    TermDatesStale,
    clamp,
    merge_and_validate,
    policy_phase,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
FULL = frozenset({"view", "operate", "console", "snapshot", "file_transfer"})
DOWNGRADED = frozenset({"view", "console", "snapshot", "file_transfer"})


def _phase(**kwargs):
    defaults = dict(source="orcahub", term_ended_at=None, access_ends_at=None, permissions=FULL, now=NOW)
    defaults.update(kwargs)
    return policy_phase(**defaults)


def test_a_policy_without_a_term_is_active() -> None:
    assert _phase().phase == ACTIVE


def test_a_term_ending_within_two_weeks_is_announced() -> None:
    assert _phase(term_ended_at=NOW + timedelta(days=13)).phase == ENDING
    assert _phase(term_ended_at=NOW + timedelta(days=15)).phase == ACTIVE


def test_a_term_that_ended_is_read_only_until_access_ends() -> None:
    state = _phase(term_ended_at=NOW - timedelta(days=1), access_ends_at=NOW + timedelta(days=29))
    assert state.phase == READ_ONLY and state.read_only and not state.inferred


def test_access_past_its_end_is_ended() -> None:
    assert _phase(term_ended_at=NOW - timedelta(days=31), access_ends_at=NOW - timedelta(seconds=1)).phase == ENDED


def test_an_orcahub_downgrade_without_dates_is_read_only_by_inference() -> None:
    state = _phase(permissions=DOWNGRADED)
    assert state.phase == READ_ONLY and state.inferred
    assert _phase(permissions=DOWNGRADED, source="local").phase == ACTIVE, "a partner's own narrow policy is just a policy"


@pytest.mark.parametrize("perms", [FULL, DOWNGRADED, frozenset({"console"})])
def test_after_the_term_only_viewing_survives(perms) -> None:
    assert clamp(perms, term_ended_at=NOW - timedelta(minutes=1), now=NOW) == perms & {"view"}


def test_before_the_term_ends_nothing_is_taken() -> None:
    assert clamp(FULL, term_ended_at=NOW + timedelta(minutes=1), now=NOW) == FULL
    assert clamp(FULL, term_ended_at=None, now=NOW) == FULL


def test_a_naive_stored_value_is_read_as_utc() -> None:
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert clamp(FULL, term_ended_at=naive, now=NOW) == {"view"}


# ── dates received from the integration ────────────────────────────────────────────────────


def test_an_absent_field_keeps_and_a_null_clears() -> None:
    stored = TermDates(NOW - timedelta(days=1), NOW + timedelta(days=29), NOW - timedelta(days=1))
    assert merge_and_validate(stored, {}, now=NOW) == stored
    cleared = merge_and_validate(stored, {"term_ended_at": None, "access_ends_at": None}, now=NOW)
    assert cleared.term_ended_at is None and cleared.access_ends_at is None


def test_dates_are_normalised_to_utc() -> None:
    jakarta = timezone(timedelta(hours=7))
    merged = merge_and_validate(TermDates(), {"term_ended_at": datetime(2026, 11, 20, 7, tzinfo=jakarta)}, now=NOW)
    assert merged.term_ended_at == datetime(2026, 11, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "received",
    [
        {"term_ended_at": datetime(2026, 11, 20)},
        {"term_ended_at": NOW + timedelta(days=10), "access_ends_at": NOW + timedelta(days=5)},
        {"access_ends_at": NOW + timedelta(days=3651)},
    ],
    ids=["naive", "access-before-term", "ten-years-ahead"],
)
def test_dates_that_cannot_be_a_term_are_refused(received) -> None:
    with pytest.raises(TermDatesInvalid):
        merge_and_validate(TermDates(), received, now=NOW)


def test_an_older_decision_arriving_late_is_refused() -> None:
    stored = TermDates(as_of=NOW)
    with pytest.raises(TermDatesStale):
        merge_and_validate(stored, {"as_of": NOW - timedelta(minutes=5), "term_ended_at": None}, now=NOW)


def test_past_dates_are_accepted_so_a_replay_stays_idempotent() -> None:
    merged = merge_and_validate(TermDates(), {"term_ended_at": NOW - timedelta(days=3)}, now=NOW)
    assert merged.term_ended_at == NOW - timedelta(days=3)


def test_retention_can_never_include_operate() -> None:
    assert "operate" not in tenant_term.RETENTION_PERMISSIONS
    assert tenant_term.RETENTION_PERMISSIONS == {"view"}
