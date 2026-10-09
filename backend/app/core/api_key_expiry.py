"""When an integration API key stops being a credential, and the word its state is reported in.

`has_expired` is the decision authentication makes (`validate_api_key`), and the one a callback,
a refresh, a rotation and a bookkeeping write use to skip a dead key. `classify_key` and
`expiry_state` report a key in one word for the owner-key surfaces from that same decision, so a
badge or a refusal never disagrees with what authentication does.

important: only None means the key never expires. A truthiness test read an empty string as
never-expiring, and an offset-bearing or unparseable value raised out of the auth path. A naive
value is UTC, as create_api_key writes it. A value that cannot be read counts as expired.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

# How close to its expiry an active key reads as `expiring`: the window in which the
# organizations it owns can still be moved before their sync stops.
EXPIRY_WARNING_DAYS = 14


def _parse(expires_at: Any) -> Optional[datetime]:
    """A stored expiry as a naive UTC datetime, None for never. Raises for anything unreadable."""
    if expires_at is None:
        return None
    moment = datetime.fromisoformat(str(expires_at).strip())
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def has_expired(expires_at: Any, now: Optional[datetime] = None) -> bool:
    """Whether a stored API key expiry has passed. `now` is naive UTC, as utcnow() gives."""
    try:
        moment = _parse(expires_at)
    except (ValueError, OverflowError):
        return True
    if moment is None:
        return False
    return (now or datetime.utcnow()) > moment


def is_unusable(metadata: Mapping[str, Any]) -> bool:
    """A revoked, inactive or expired key. Its integration is gone, so nothing calls it back."""
    return bool(
        metadata.get("is_revoked")
        or not metadata.get("is_active", True)
        or has_expired(metadata.get("expires_at"))
    )


def classify_key(metadata: Any) -> str:
    """One word for a stored key: `revoked`, `expired`, `inactive` or `active`.

    note: the order is the precedence. A revocation is a decision someone made and outranks a
    date, and an expiry outranks `is_active`, which nothing sets false when a key lapses.
    """
    if not isinstance(metadata, Mapping):
        return "expired"
    if metadata.get("is_revoked"):
        return "revoked"
    if has_expired(metadata.get("expires_at")):
        return "expired"
    if not metadata.get("is_active", True):
        return "inactive"
    return "active"


def expiry_state(value: Any, now: Optional[datetime] = None, warn_within_days: int = EXPIRY_WARNING_DAYS) -> str:
    """How a key's expiry stands: `never`, `expired`, `expiring` or `active`."""
    if has_expired(value, now):
        return "expired"
    moment = _parse(value)
    if moment is None:
        return "never"
    remaining = (moment - (now or datetime.utcnow())) // timedelta(days=1)
    return "expiring" if remaining <= warn_within_days else "active"
