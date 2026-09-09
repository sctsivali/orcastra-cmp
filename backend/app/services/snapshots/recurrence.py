"""When a schedule fires, as pure arithmetic.

No clock of its own, no I/O, no database. Every function takes the moment it should reason about,
which is what makes a three-day outage testable without waiting three days.

Two properties this file exists to hold.

**Slots are anchored, never chained.** A slot is a property of the calendar and the schedule, not of
when the last run happened. Computing the next run as `finished_at + period` lets a run that took
forty seconds push every future run forty seconds later, forever, and the drift is invisible until
an operator notices their nightly snapshot has wandered into the working day.

**UTC only, and the field names say so.** A local-time schedule at 02:30 either fires twice or not
at all on a daylight-saving transition, and the arithmetic that gets that right is exactly the
arithmetic nobody exercises. UTC has no transitions. An operator in Jakarta wanting 03:00 local
picks 20:00 UTC, and the console renders the label with both. That is a display problem, not a
storage one.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# The closed vocabulary. A cron string would be more expressive and would also need a parser, a
# dependency, and a validator for a syntax an operator would have to be taught. Four shapes cover
# what an operator console is actually asked for, and a Select cannot produce an invalid value.
FREQUENCIES: Dict[str, int] = {
    "every_6_hours": 6,
    "every_12_hours": 12,
    "daily": 24,
    "weekly": 168,
}

# Quarter hours only. This is the one knob that spreads load across a fleet, and four values an
# operator picks from a list spread it as well as sixty do while keeping the vocabulary small
# enough to reason about.
MINUTES: tuple = (0, 15, 30, 45)

# 2000-01-03 was a Monday, which makes it the natural origin for a weekday-anchored schedule.
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

    The database carries the same rules as CHECK constraints. Both, deliberately: the constraint is
    what keeps a row written by anything other than this service honest, and the message is what an
    operator can act on.
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
        # Not pedantry: a weekday stored against a daily schedule is a value the console would
        # render and the runner would ignore, which is how a schedule comes to say one thing and
        # do another.
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

    This is what a run names itself after, and it is why a backend that was down for a day takes
    ONE snapshot named for today rather than a day's worth named for yesterday.
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

    Used to count what an outage cost. Nothing is replayed: every catch-up snapshot would capture
    the same state, the current one, so five of them are five restore points wearing misleading
    timestamps rather than five moments anyone can go back to.
    """
    anchor = _anchor(frequency, hour_utc, minute_utc, weekday)
    step = period(frequency)
    return _index(_as_utc(end), anchor, step) - _index(_as_utc(start), anchor, step)


def _as_utc(moment: datetime) -> datetime:
    """A naive datetime is read as UTC rather than rejected.

    Postgres hands back an aware value for a timestamptz column, but a value that has been through
    a serialisation boundary may not be, and refusing it here would turn a formatting detail into a
    schedule that stops.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def describe(frequency: str, hour_utc: int, minute_utc: int, weekday: Optional[int]) -> str:
    """One sentence naming when this fires, for an audit record and an API response.

    The console composes its own copy from the fields; this exists so a log line and a refusal read
    the same way as the screen without the reader holding four numbers in their head.
    """
    at = f"{hour_utc:02d}:{minute_utc:02d} UTC"
    if frequency == "weekly":
        return f"every {WEEKDAY_NAMES[int(weekday or 0)]} at {at}"
    if frequency == "daily":
        return f"daily at {at}"
    return f"every {period_hours(frequency)} hours, on the hour of {at}"
