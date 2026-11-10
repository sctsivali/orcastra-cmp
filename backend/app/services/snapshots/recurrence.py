"""When a schedule fires, as pure arithmetic: no clock, no I/O, no database.

Every function takes the moment it reasons about, so a multi-day outage is testable directly.

Slots are anchored to the calendar and the schedule, never chained from the last run. Computing the
next run as `finished_at + period` would let a run that took forty seconds push every later run
forty seconds further. Times are UTC only, and the field names say so: a local-time schedule fires
twice or not at all on a daylight-saving transition. The console renders the label in both UTC and
local time.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# The closed vocabulary. A cron string would need a parser, a dependency and a validator for a
# syntax operators would have to learn. Four shapes cover what operators ask for, and a Select
# cannot produce an invalid value.
FREQUENCIES: Dict[str, int] = {
    "every_6_hours": 6,
    "every_12_hours": 12,
    "daily": 24,
    "weekly": 168,
}

# Quarter hours only. The minute is what spreads load across a fleet, and four values spread it
# as well as sixty while keeping the vocabulary small.
MINUTES: tuple = (0, 15, 30, 45)

# Weekday-anchored schedules count from a Monday, `2000-01-03`.
_WEEK_ORIGIN = datetime(2000, 1, 3, tzinfo=timezone.utc)
_DAY_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)

WEEKDAY_NAMES: List[str] = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


class RecurrenceError(ValueError):
    """A recurrence was refused before it could be stored."""


def period(frequency: str) -> timedelta:
    """How long one turn of this schedule is."""
    hours = FREQUENCIES.get(frequency)
    if hours is None:
        raise RecurrenceError(
            f"Unknown frequency '{frequency}'. Expected one of {', '.join(sorted(FREQUENCIES))}."
        )
    return timedelta(hours=hours)


def period_hours(frequency: str) -> int:
    return int(period(frequency).total_seconds() // 3600)


def validate(
    frequency: str,
    hour_utc: int,
    minute_utc: int,
    weekday: Optional[int],
) -> None:
    """Refuse a recurrence that cannot be stored, naming the field that is wrong.

    The database carries the same rules as CHECK constraints, which hold for rows written by
    anything else; this message is what an operator can act on.
    """
    period(frequency)  # raises on an unknown frequency
    if not isinstance(hour_utc, int) or not 0 <= hour_utc <= 23:
        raise RecurrenceError("hour_utc must be an hour of the day, 0 to 23.")
    if minute_utc not in MINUTES:
        raise RecurrenceError(
            f"minute_utc must be one of {', '.join(str(m) for m in MINUTES)}."
        )
    if frequency == "weekly":
        if weekday is None:
            raise RecurrenceError("A weekly schedule needs a weekday.")
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise RecurrenceError("weekday must be 0 (Monday) through 6 (Sunday).")
    elif weekday is not None:
        # A weekday stored on a non-weekly schedule would be rendered by the console and ignored
        # by the runner.
        raise RecurrenceError("Only a weekly schedule takes a weekday.")


def _anchor(frequency: str, hour_utc: int, minute_utc: int, weekday: Optional[int]) -> datetime:
    """One fixed point every slot of this schedule is an exact number of periods away from."""
    if frequency == "weekly":
        return _WEEK_ORIGIN + timedelta(
            days=int(weekday or 0), hours=hour_utc, minutes=minute_utc
        )
    # 24 divides evenly by 6 and 12, so anchoring on a day boundary keeps every sub-daily slot on
    # an hour congruent to hour_utc. `daily` falls out of the same expression with no special case.
    hours = period_hours(frequency)
    return _DAY_ORIGIN + timedelta(hours=hour_utc % hours, minutes=minute_utc)


def _index(moment: datetime, anchor: datetime, step: timedelta) -> int:
    """How many whole periods `moment` is past `anchor`, rounding toward minus infinity."""
    return (moment - anchor) // step


def next_slot_after(
    frequency: str,
    hour_utc: int,
    minute_utc: int,
    weekday: Optional[int],
    after: datetime,
) -> datetime:
    """The first slot strictly later than `after`.

    Strictly, so a schedule that has just fired at its own slot does not immediately claim the same
    one again.
    """
    anchor = _anchor(frequency, hour_utc, minute_utc, weekday)
    step = period(frequency)
    return anchor + (_index(_as_utc(after), anchor, step) + 1) * step


def latest_slot_at_or_before(
    frequency: str,
    hour_utc: int,
    minute_utc: int,
    weekday: Optional[int],
    moment: datetime,
) -> datetime:
    """The most recent slot that has already come round.

    A run names itself after this slot, so a backend that was down for a day takes one snapshot
    named for today rather than a day's worth named for yesterday.
    """
    anchor = _anchor(frequency, hour_utc, minute_utc, weekday)
    step = period(frequency)
    return anchor + _index(_as_utc(moment), anchor, step) * step


def slots_between(
    frequency: str,
    hour_utc: int,
    minute_utc: int,
    weekday: Optional[int],
    start: datetime,
    end: datetime,
) -> int:
    """How many slots fall after `start` and at or before `end`.

    Counts what an outage cost. Missed slots are not replayed: every catch-up snapshot would
    capture the same current state under a misleading timestamp.
    """
    anchor = _anchor(frequency, hour_utc, minute_utc, weekday)
    step = period(frequency)
    return _index(_as_utc(end), anchor, step) - _index(_as_utc(start), anchor, step)


def _as_utc(moment: datetime) -> datetime:
    """A naive datetime is read as UTC rather than rejected.

    Postgres returns an aware value for a timestamptz column, but a value that crossed a
    serialisation boundary may be naive, and refusing it would stop the schedule.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def describe(frequency: str, hour_utc: int, minute_utc: int, weekday: Optional[int]) -> str:
    """One sentence naming when this fires, for an audit record and an API response.

    The console composes its own copy from the fields; this keeps log lines and refusals worded
    like the screen.
    """
    at = f"{hour_utc:02d}:{minute_utc:02d} UTC"
    if frequency == "weekly":
        return f"every {WEEKDAY_NAMES[int(weekday or 0)]} at {at}"
    if frequency == "daily":
        return f"daily at {at}"
    return f"every {period_hours(frequency)} hours, on the hour of {at}"
