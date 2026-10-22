"""A cached response body together with when it was written.

Stale-while-revalidate needs the age of an entry, and Redis cannot report it: an entry's
remaining TTL does not say when it was written once two writes can carry different TTLs (a
degraded body is kept for less time than a complete one). So the write time travels inside the
value, in the same key, which keeps it atomic with the body and lets every existing delete and
pattern sweep remove both at once.

Shape: {"__swr__": 1, "t": stored_at, "b": build_started_at, "s": fresh_for, "v": body}

A value without the marker is a plain body written by code that predates this module. It is
treated as fresh with an unknown age, so nothing that reads an old entry changes behaviour.
"""

from dataclasses import dataclass
from typing import Any, Optional

MARK = "__swr__"


@dataclass(frozen=True)
class Entry:
    """One cached value and, when it was written through the envelope, its timing."""

    value: Any
    stored_at: Optional[float] = None
    build_started_at: Optional[float] = None
    fresh_for: Optional[int] = None

    def age(self, now: float) -> Optional[float]:
        return None if self.stored_at is None else max(0.0, now - self.stored_at)

    def is_fresh(self, now: float) -> bool:
        """True while the entry is inside its soft TTL; an entry of unknown age counts as fresh."""
        if self.stored_at is None or self.fresh_for is None:
            return True
        return now - self.stored_at <= self.fresh_for


def wrap(body: Any, *, stored_at: float, build_started_at: float, fresh_for: int) -> dict:
    return {MARK: 1, "t": stored_at, "b": build_started_at, "s": fresh_for, "v": body}


def is_envelope(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get(MARK) == 1 and "v" in raw


def unwrap(raw: Any) -> Entry:
    """The entry a raw cached value describes; a plain body becomes an entry of unknown age."""
    if not is_envelope(raw):
        return Entry(value=raw)
    return Entry(
        value=raw["v"],
        stored_at=_number(raw.get("t")),
        build_started_at=_number(raw.get("b")),
        fresh_for=_int(raw.get("s")),
    )


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
