"""`GET /extensions/self` is the endpoint every doc tells an add-on to poll, and it said nothing
about the credential lapsing.

Its own docstring calls it the reconcile backstop: poll this rather than assume the grant you were
given is the grant you hold. But an installation credential lives 365 days and the first signal it
had ended was a 401 in production, which is exactly the failure `expiry_state` was written to
prevent. `whoami` already answered this, under a different name, on a route no extensions page
recommends.

The handler is exercised rather than grepped: a structural check would pass against a response that
computed the field and then dropped it.
"""

import asyncio
from datetime import timedelta

import pytest

from app.core.api_key_expiry import now_utc, utc_isoformat
from app.routers import extensions_external


class Row:
    """One `.scalar_one()`-shaped answer."""

    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class FakeInstall:
    id = 7
    extension_id = 1
    organization_id = 3
    status = "active"
    granted_capabilities = ["inventory.read"]
    granted_scope = [{"cluster_id": "nuc2", "permissions": ["read"]}]
    settings: dict = {}
    credential_generation = 1


class FakeExtension:
    slug = "acme-backup"
    name = "Acme Backup"
    version = "1.0.0"


class FakeOrganization:
    slug = "acme"
    name = "Acme Ltd"


class FakeDB:
    """Answers the three selects the handler makes, in the order it makes them."""

    def __init__(self) -> None:
        self._answers = [Row(FakeInstall()), Row(FakeExtension()), Row(FakeOrganization())]

    async def execute(self, _statement):
        return self._answers.pop(0)


@pytest.fixture(autouse=True)
def _no_vault(monkeypatch):
    monkeypatch.setattr(extensions_external, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(
        extensions_external.extension_settings, "read_resolved_settings", lambda *_a: {}
    )


def _describe(expires_at):
    return asyncio.run(
        extensions_external.describe_self(
            api_key={"id": "oak_x", "expires_at": expires_at}, db=FakeDB()
        )
    )


def test_it_reports_when_the_credential_lapses() -> None:
    expires = utc_isoformat(now_utc() + timedelta(days=30))

    body = _describe(expires)

    assert body["credential"]["expires_at"] == expires
    # Floors, and that is the honest direction: a credential with 29 days and 23 hours left has
    # 29 whole days, and rounding up would tell an add-on it has a day it does not.
    assert body["credential"]["expires_in_days"] == 29
    assert body["credential"]["state"] == "active"


def test_it_warns_before_the_401_rather_than_after() -> None:
    """The whole point: a window in which an add-on can act, not a post-mortem."""
    body = _describe(utc_isoformat(now_utc() + timedelta(days=3)))

    assert body["credential"]["state"] == "expiring", (
        "a credential three days from lapsing reads as healthy, so the first signal is still the 401"
    )
    assert body["credential"]["expires_in_days"] == 2


def test_a_key_that_never_expires_says_so_rather_than_reporting_zero() -> None:
    """`never` is a real answer. Reporting 0 days would read as expiring today."""
    body = _describe(None)

    assert body["credential"]["state"] == "never"
    assert body["credential"]["expires_in_days"] is None
    assert body["credential"]["expires_at"] is None


def test_an_expired_credential_is_named_as_expired() -> None:
    body = _describe(utc_isoformat(now_utc() - timedelta(days=1)))

    assert body["credential"]["state"] == "expired"


def test_the_rest_of_the_answer_is_unchanged() -> None:
    """The positive twin for every assertion above: the endpoint still answers what it always did."""
    body = _describe(utc_isoformat(now_utc() + timedelta(days=30)))

    assert body["installed"] is True
    assert body["installation_id"] == 7
    assert body["extension"]["slug"] == "acme-backup"
    assert body["granted_capabilities"] == ["inventory.read"]
    assert body["credential_generation"] == 1
