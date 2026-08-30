"""When an API key stops being a credential.

The enforcement itself was correct for the data that exists: every `expires_at` in this
deployment was written by `create_api_key`, which emits a naive UTC ISO string, and the check
compared it against a naive `datetime.utcnow()`. Naive met naive and the arithmetic was right.

What it could not survive was any other shape. Measured against the running deployment before
this module existed, by writing each value into Vault and presenting the key:

    '2020-01-01T00:00:00'        -> 401, correct
    '2020-01-01T00:00:00Z'       -> 500
    '2020-01-01T00:00:00+00:00'  -> 500
    '2099-01-01T00:00:00Z'       -> 500   (a key that had NOT expired)
    'not-a-date'                 -> 500
    ''                           -> 200   (accepted, expiry skipped entirely)

The 500s are a `TypeError` from comparing an offset-aware datetime to a naive one, and a
`ValueError` from the parse, neither of which had a handler. The last line is the sharper one:
the caller tested `if expires_at:`, so an empty string read as "never expires" and the key was
accepted. That is the only fail-open shape, and it is why this module decides rather than the
call site.

Two rules, both deliberate:

- **Naive means UTC.** Every writer in this repo emits naive UTC, so stamping a naive value with
  UTC preserves the meaning of everything already stored. New values are written with an offset
  so the convention stops being implicit.
- **Unreadable means expired.** If the server cannot tell whether a credential is still valid,
  it is not. Anything that is not None and not parseable is refused, empty string included.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MalformedExpiry",
    "classify_key",
    "tally_keys",
    "days_remaining",
    "expiry_state",
    "is_expired",
    "now_utc",
    "parse_expiry",
    "utc_isoformat",
]


class MalformedExpiry(ValueError):
    """A stored expiry that cannot be read. Callers treat this as expired."""


def now_utc() -> datetime:
    """The clock every comparison here uses. Aware, so it cannot meet a naive value by accident."""
    return datetime.now(timezone.utc)


def utc_isoformat(moment: Optional[datetime] = None) -> str:
    """How a new timestamp is written: aware, so the stored form carries its own meaning."""
    return (moment or now_utc()).astimezone(timezone.utc).isoformat()


def parse_expiry(value: Any) -> Optional[datetime]:
    """A stored expiry as an aware UTC datetime, or None when the key never expires.

    Raises MalformedExpiry for anything else, including the empty string, rather than letting a
    caller's truthiness test turn it into "no expiry".
    """
    if value is None:
        return None

    # important: the conversion is inside the guard, not after it. `astimezone` on an extreme
    # date raises OverflowError, which is NOT a subclass of ValueError, so it escaped an
    # `except ValueError` and reached the caller uncaught. That is the same shape as the defect
    # this module exists to remove: `'9999-12-31T23:59:59-23:00'` produced a 500 on the
    # authentication path and, through classify_key, a 500 on the whole key list for that org.
    try:
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                raise MalformedExpiry("expires_at is an empty string, which states nothing")
            # note: Python 3.11+ accepts a `Z` suffix directly, and the replace is a no-op there.
            # It is kept for the older spellings that reached Vault before that.
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            raise MalformedExpiry(f"expires_at is a {type(value).__name__}, not a timestamp")

        if moment.tzinfo is None:
            # Every writer here emits naive UTC, so this preserves what is already stored.
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except MalformedExpiry:
        raise
    except (ValueError, OverflowError, OSError) as error:
        raise MalformedExpiry(f"expires_at is not a usable timestamp: {value!r}") from error


def is_expired(value: Any, now: Optional[datetime] = None) -> bool:
    """Whether a key with this stored expiry may still be used.

    important: a value that cannot be read counts as expired. The alternative is accepting a
    credential whose validity the server was unable to establish, which is the failure mode this
    module exists to remove.
    """
    try:
        expires_at = parse_expiry(value)
    except MalformedExpiry as error:
        logger.warning("Refusing a key with an unreadable expiry: %s", error)
        return True
    if expires_at is None:
        return False
    return (now or now_utc()) >= expires_at


def days_remaining(value: Any, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days left before this expiry, or None when it never expires.

    Negative once it has passed, so a caller can tell "expired three days ago" from "expires in
    three days" without a second field. Rounds down, so 23 hours left reads as 0 rather than 1.
    """
    try:
        expires_at = parse_expiry(value)
    except MalformedExpiry:
        return None
    if expires_at is None:
        return None
    return (expires_at - (now or now_utc())) // timedelta(days=1)


def tally_keys(records: Any, now: Optional[datetime] = None) -> Dict[str, int]:
    """How many keys stand in each state, counted from the same precedence the badges render.

    important: this is a function rather than a loop inside the handler so it can be asserted on
    fixtures. The loop it replaces was pinned only by a structural test that checked the counter
    was incremented, which stayed green when the branch bodies were swapped and every count was
    wrong.
    """
    counts = {"active": 0, "expired": 0, "revoked": 0, "inactive": 0}
    for record in records or []:
        counts[classify_key(record, now)] += 1
    return counts


def classify_key(metadata: Any, now: Optional[datetime] = None) -> str:
    """One word for a stored key: `revoked`, `expired`, `inactive` or `active`.

    important: the order is the precedence. Revocation is a decision someone made and outranks a
    date; expiry outranks the `is_active` flag, because that flag is only ever written at
    creation and by `revoke_api_key`, so nothing ever set it false when a key lapsed. Six keys in
    this deployment were past their expiry, still flagged active, and counted among the active
    ones in the list response.
    """
    if not isinstance(metadata, dict):
        return "expired"
    if metadata.get("is_revoked", False):
        return "revoked"
    if is_expired(metadata.get("expires_at"), now):
        return "expired"
    if not metadata.get("is_active", True):
        return "inactive"
    return "active"


def expiry_state(value: Any, now: Optional[datetime] = None, warn_within_days: int = 14) -> str:
    """One word for how a key stands: `never`, `expired`, `expiring` or `active`.

    `expiring` is what the operator surface needs and had no way to compute: today the first
    signal that a key is about to lapse is a 401 in production.
    """
    if is_expired(value, now):
        return "expired"
    remaining = days_remaining(value, now)
    if remaining is None:
        return "never"
    return "expiring" if remaining <= warn_within_days else "active"
