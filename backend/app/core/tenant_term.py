"""The paid term behind a tenant access policy: which phase it is in and what it still grants.

OrcaHub sells a buyer a project for a paid term, then keeps the data for a retention window
before deleting it. Orcastra records two optional dates on the buyer's `auto:<project>` policy:

- `term_ended_at`: the paid term is over. The policy grants viewing only, whatever permissions
  the rule holds.
- `access_ends_at`: the retention window is over. The policy grants nothing.

A policy OrcaHub narrowed without dates (its downgrade drops `operate`) is read-only by
inference; that changes the wording the tenant sees, never what is enforced. Everything here is
pure: dates in, a phase and a permission set out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AbstractSet, Mapping, Optional

ENDING_NOTICE = timedelta(days=14)
MAX_AHEAD = timedelta(days=3650)
RETENTION_PERMISSIONS = frozenset({"view"})

ACTIVE = "active"
ENDING = "ending"
READ_ONLY = "read_only"
ENDED = "ended"


class TermDatesInvalid(ValueError):
    """Dates that cannot describe a real term; refused before anything is written."""


class TermDatesStale(ValueError):
    """An older decision about the term arrived after a newer one was recorded."""


@dataclass(frozen=True)
class TermDates:
    term_ended_at: Optional[datetime] = None
    access_ends_at: Optional[datetime] = None
    as_of: Optional[datetime] = None


@dataclass(frozen=True)
class TermState:
    phase: str
    inferred: bool
    term_ended_at: Optional[datetime]
    access_ends_at: Optional[datetime]

    @property
    def read_only(self) -> bool:
        return self.phase == READ_ONLY


def utc(value: Optional[datetime]) -> Optional[datetime]:
    """A stored or received instant in UTC. A naive value is refused: it names no instant."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise TermDatesInvalid("Term dates must carry a time zone, for example 2026-11-20T00:00:00Z")
    return value.astimezone(timezone.utc)


def _stored(value: Optional[datetime]) -> Optional[datetime]:
    # SQLite test sessions return naive values for timestamptz columns; they were stored as UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def policy_phase(
    *,
    source: Optional[str],
    term_ended_at: Optional[datetime],
    access_ends_at: Optional[datetime],
    permissions: AbstractSet[str],
    now: datetime,
) -> TermState:
    term_ended_at, access_ends_at = _stored(term_ended_at), _stored(access_ends_at)
    if access_ends_at is not None and access_ends_at <= now:
        phase, inferred = ENDED, False
    elif term_ended_at is not None and term_ended_at <= now:
        phase, inferred = READ_ONLY, False
    elif term_ended_at is not None and term_ended_at - now <= ENDING_NOTICE:
        phase, inferred = ENDING, False
    elif term_ended_at is None and access_ends_at is not None and access_ends_at - now <= ENDING_NOTICE:
        phase, inferred = ENDING, False
    elif (
        source == "orcahub"
        and term_ended_at is None
        and access_ends_at is None
        and "view" in permissions
        and "operate" not in permissions
    ):
        phase, inferred = READ_ONLY, True
    else:
        phase, inferred = ACTIVE, False
    return TermState(phase, inferred, term_ended_at, access_ends_at)


def clamp(permissions: AbstractSet[str], *, term_ended_at: Optional[datetime], now: datetime) -> frozenset:
    """What a rule still grants once its policy's paid term is over: viewing, at most."""
    term_ended_at = _stored(term_ended_at)
    if term_ended_at is not None and term_ended_at <= now:
        return frozenset(permissions) & RETENTION_PERMISSIONS
    return frozenset(permissions)


def merge_and_validate(
    stored: TermDates,
    received: Mapping[str, Optional[datetime]],
    *,
    now: datetime,
) -> TermDates:
    """The dates after applying what was received to what is stored.

    A key absent from `received` leaves the stored value; a key present with None clears it.
    Raises TermDatesStale for an `as_of` older than the stored one, TermDatesInvalid for a naive
    date, an access end before the term end, or a date more than ten years ahead.
    """
    as_of = utc(received.get("as_of")) if "as_of" in received else None
    if as_of is not None and stored.as_of is not None and as_of < _stored(stored.as_of):
        raise TermDatesStale("A newer decision about this term is already recorded")
    term_ended_at = utc(received["term_ended_at"]) if "term_ended_at" in received else _stored(stored.term_ended_at)
    access_ends_at = (
        utc(received["access_ends_at"]) if "access_ends_at" in received else _stored(stored.access_ends_at)
    )
    for label, value in (("term_ended_at", term_ended_at), ("access_ends_at", access_ends_at)):
        if value is not None and value - now > MAX_AHEAD:
            raise TermDatesInvalid(f"{label} is more than ten years ahead")
    if term_ended_at is not None and access_ends_at is not None and access_ends_at < term_ended_at:
        raise TermDatesInvalid("access_ends_at cannot be before term_ended_at")
    return TermDates(term_ended_at, access_ends_at, as_of if as_of is not None else _stored(stored.as_of))
