"""An unattended key must not reach further than a human would, and consent is not the integrator's to give.

Two gaps this closes, both structural rather than a coding slip.

A human admin cannot open a console into a cluster they do not own without a break-glass grant:
justified, time-boxed, revocable, and visible to the tenant in the session audit. An API key had
no equivalent AND no way to acquire one, because `break_glass_grants` is keyed on `admin_email`
and `enforce_sensitive_access` takes an authenticated user. So a partner could issue a key, tick a
capability, and hold permanent unattended access with no reason recorded and nothing to revoke.

Separately, a capability on a key is a decision made by whoever issues the key. Whether an
organization agrees that an agent may photograph its desktops is a decision belonging to that
organization. Screen contents leaving for a third-party model is a disclosure to a sub-processor,
so the two consents are kept apart and both are required.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from app.models.db_models import BreakGlassGrant, DesktopControlPolicy
from app.services.desktop_policy import DENIED, DesktopConsent, get_consent, set_consent


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Session:
    """Enough AsyncSession to exercise the query paths without a database."""

    def __init__(self, row: Any = None) -> None:
        self._row = row
        self.added: List[Any] = []
        self.commits = 0

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._row)

    def add(self, row: Any) -> None:
        self.added.append(row)
        self._row = row

    async def commit(self) -> None:
        self.commits += 1


# ── the grant may now name a key, and only one principal ───────────────────────────────────────

def test_a_grant_names_exactly_one_kind_of_principal() -> None:
    """Neither would authorize nobody; both would be ambiguous, and the enforcement query would
    match on either."""
    constraints = {c.name for c in BreakGlassGrant.__table__.constraints if c.name}
    assert "ck_bgg_one_principal" in constraints


def test_the_key_principal_column_exists_and_is_optional() -> None:
    columns = BreakGlassGrant.__table__.columns
    assert columns["api_key_id"].nullable is True
    # Widened, because a key grant has no human behind it and encoding one as an email would make
    # every existing query silently match the wrong kind of principal.
    assert columns["admin_email"].nullable is True


def test_a_grant_records_the_registration_it_was_issued_against() -> None:
    """cluster_id is a free-text Vault key with no foreign key, so a cluster deleted and
    re-registered under another owner matches an old grant by string alone."""
    assert "cluster_fingerprint" in BreakGlassGrant.__table__.columns


@pytest.mark.asyncio
async def test_a_key_grant_is_refused_when_the_cluster_is_a_different_registration() -> None:
    from app.services.break_glass_service import get_active_key_grant

    grant = BreakGlassGrant(
        api_key_id="oak_a",
        cluster_id="nuc2",
        cluster_fingerprint="aa" * 32,
        reason="support",
        created_by="ops",
        expires_at=_now() + timedelta(hours=1),
    )
    session = _Session(grant)

    assert await get_active_key_grant(session, "oak_a", "nuc2", fingerprint="aa" * 32) is grant
    assert await get_active_key_grant(session, "oak_a", "nuc2", fingerprint="bb" * 32) is None


@pytest.mark.asyncio
async def test_a_grant_issued_before_fingerprints_existed_still_works() -> None:
    """It never made that promise, so it is not held to it."""
    from app.services.break_glass_service import get_active_key_grant

    grant = BreakGlassGrant(
        api_key_id="oak_a",
        cluster_id="nuc2",
        cluster_fingerprint=None,
        reason="support",
        created_by="ops",
        expires_at=_now() + timedelta(hours=1),
    )
    session = _Session(grant)
    assert await get_active_key_grant(session, "oak_a", "nuc2", fingerprint="cc" * 32) is grant


@pytest.mark.asyncio
async def test_no_grant_means_no_access() -> None:
    from app.services.break_glass_service import get_active_key_grant

    assert await get_active_key_grant(_Session(None), "oak_a", "nuc2") is None


# ── the organization's own consent ─────────────────────────────────────────────────────────────

def test_input_requires_the_feature_to_be_on_at_all() -> None:
    """allow_input alone would let a disabled policy still authorize typing, which reads as a bug
    the first time somebody turns desktop control off and expects everything to stop."""
    assert DesktopConsent(enabled=True, allow_input=True).may_input is True
    assert DesktopConsent(enabled=False, allow_input=True).may_input is False
    assert DesktopConsent(enabled=True, allow_input=False).may_input is False


def test_viewing_and_typing_are_separate_consents() -> None:
    """A support desk that may look at a stuck screen has not thereby been allowed to type."""
    watching = DesktopConsent(enabled=True, allow_input=False)
    assert watching.may_view is True
    assert watching.may_input is False


@pytest.mark.asyncio
async def test_an_organization_that_was_never_asked_has_not_agreed() -> None:
    assert await get_consent(_Session(None), 7) == DENIED


@pytest.mark.asyncio
async def test_a_cluster_with_no_organization_is_refused() -> None:
    """Nobody could have consented, so an unbound cluster must not be the soft spot."""
    assert await get_consent(_Session(None), None) == DENIED


@pytest.mark.asyncio
async def test_consent_is_read_back_as_stored() -> None:
    row = DesktopControlPolicy(organization_id=6, enabled=True, allow_input=True)
    assert await get_consent(_Session(row), 6) == DesktopConsent(enabled=True, allow_input=True)


@pytest.mark.asyncio
async def test_turning_the_feature_off_clears_the_input_consent() -> None:
    """Otherwise turning it back on later silently restores a stronger permission than expected."""
    row = DesktopControlPolicy(organization_id=6, enabled=True, allow_input=True)
    session = _Session(row)
    result = await set_consent(
        session, 6, enabled=False, allow_input=True, updated_by="ops@sivali.co.id"
    )
    assert result.enabled is False
    assert result.allow_input is False
    assert row.allow_input is False


@pytest.mark.asyncio
async def test_the_first_decision_creates_the_row() -> None:
    session = _Session(None)
    await set_consent(session, 9, enabled=True, allow_input=False, updated_by="ops@sivali.co.id")
    assert len(session.added) == 1
    assert session.added[0].organization_id == 9
    assert session.commits == 1


def test_the_policy_is_not_stored_in_the_personalization_blob() -> None:
    """Its own table on purpose. organization_preferences.settings is the JSONB the branding
    editor writes, and this repository has already shipped bugs where one code path overwrote
    another's keys in it. A security consent cleared by somebody saving a logo is a defect class
    already paid for once here."""
    assert DesktopControlPolicy.__tablename__ == "desktop_control_policy"
    columns = DesktopControlPolicy.__table__.columns
    assert "settings" not in columns
    # A real foreign key with a cascade, unlike cluster_id elsewhere in this schema: an
    # organization that is deleted takes its consent with it rather than leaving a row a later
    # organization reusing the id would inherit.
    foreign_keys = list(columns["organization_id"].foreign_keys)
    assert foreign_keys and foreign_keys[0].ondelete == "CASCADE"
