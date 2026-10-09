"""One expiry rule for authentication and for every word a key's state is reported in.

The owner-key surfaces name an owner's state (active, expiring, expired, revoked, inactive). They
read it from app/core/api_key_expiry.py, the module authentication's own expiry check moved into,
so a badge that says "active" can never sit on a key authentication refuses.
"""

from datetime import datetime, timedelta

import pytest

from app.core import api_key_expiry
from app.core.api_key_expiry import classify_key, expiry_state, has_expired, is_unusable
from app.routers import integrations
from app.services.integration_key_ownership import key_state

NOW = datetime(2030, 1, 1, 12, 0, 0)


def test_authentication_uses_the_same_functions() -> None:
    assert integrations._api_key_has_expired is has_expired
    assert integrations._api_key_is_unusable is is_unusable


@pytest.mark.parametrize("value, expired", [
    (None, False),
    ("", True),
    ("   ", True),
    ("not-a-date", True),
    ("2029-12-31T12:00:00", True),
    ("2030-01-02T12:00:00", False),
    ("2030-01-01T18:00:00+07:00", True),
    ("2030-01-01T18:00:00-01:00", False),
    ("9999-12-31T23:59:59-23:00", True),
])
def test_only_none_never_expires_and_unreadable_is_expired(value: object, expired: bool) -> None:
    assert has_expired(value, now=NOW) is expired


def test_the_precedence_is_revoked_then_expired_then_inactive() -> None:
    past = "2020-01-01T00:00:00"
    assert classify_key({"is_revoked": True, "is_active": False, "expires_at": past}) == "revoked"
    assert classify_key({"is_active": False, "expires_at": past}) == "expired"
    assert classify_key({"is_active": False}) == "inactive"
    assert classify_key({"is_active": True, "expires_at": None}) == "active"
    assert classify_key(None) == "expired" and classify_key("not a record") == "expired"


@pytest.mark.parametrize("days, word", [(None, "never"), (-1, "expired"), (0, "expiring"), (14, "expiring"),
                                        (15, "active"), (200, "active")])
def test_expiry_state_words(days: object, word: str) -> None:
    value = None if days is None else (NOW + timedelta(days=days, hours=1)).isoformat()
    if days == -1:
        value = (NOW - timedelta(hours=1)).isoformat()
    assert expiry_state(value, now=NOW) == word


def test_the_warning_window_is_fourteen_days() -> None:
    assert api_key_expiry.EXPIRY_WARNING_DAYS == 14


def test_an_owner_key_state() -> None:
    soon = (datetime.utcnow() + timedelta(days=3)).isoformat()
    assert key_state({"is_active": True, "expires_at": soon}) == "expiring"
    assert key_state({"is_revoked": True, "expires_at": soon}) == "revoked"
    assert key_state(None) == key_state({}) == key_state({"id": "x"}, missing=True) == "missing"
    assert key_state({"is_active": True}, unknown=True) == "unknown"
