"""Withdrawing tenant.provision from a key that owns organizations is asked about, then forced.

Owner decision, 2026-10-08: not silently allowed and not refused. Without it the owner still syncs
its organizations and their members, but provisioning buyer dashboard access for them stops, and no
other key may provision for them. The capability edit answers 409 naming that consequence and the
organizations the caller may see, and the same request with force goes through, recorded as forced
with the organization ids. org.sync and inventory.read stay refused, force or not, and who may edit
the key at all is unchanged.

These send the real route through its RBAC gate against SQLite organization rows and an in-memory
Vault.
"""

from typing import Any, Dict, List

import pytest

from app.models.db_models import Organization
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.integration_key_provisioning import CONSEQUENCE
from tests.test_a_capability_grant_leaves_a_record import PARTNER, _FakeVault
from tests.test_a_key_that_owns_an_organization_is_not_revoked_or_deleted import (
    owned,  # noqa: F401
    store,
)
from tests.test_an_add_on_credential_refuses_grant_edits import (
    PLAIN,
    plain_metadata,
    surface,  # noqa: F401
)
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import (
    ADMIN,
    KEYS,
    TENANT,
    _Down,
    keys,  # noqa: F401
    send,
)

FULL = ["inventory.read", "org.sync", "tenant.provision"]
WITHOUT_PROVISIONING = ["inventory.read", "org.sync"]
ROUTE = f"{KEYS}/{PLAIN}/capabilities"


@pytest.fixture
def owner(owned: Dict[str, Any]) -> Dict[str, Any]:  # noqa: F811
    """PLAIN holds every OrcaHub capability and owns acme-cloud and fossgo; PARTNER is in acme-cloud."""
    store(owned, PLAIN, {**plain_metadata(), "capabilities": list(FULL)})
    ids = dict(owned["db"].sync.query(Organization.slug, Organization.id).all())
    return {**owned, "ids": ids}


def stored_capabilities(owner: Dict[str, Any]) -> List[str]:
    return owner["vault"].store[_FakeVault._path(PLAIN, "metadata")]["capabilities"]


async def test_withdrawing_it_without_force_answers_what_would_stop(owner: Dict[str, Any]) -> None:
    owner["scope"](PARTNER)
    response = await send(PARTNER, owner["db"], "PUT", ROUTE, {"capabilities": WITHOUT_PROVISIONING})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["error"], detail["action"], detail["capability"]) == (
        "provisioning_would_stop", "capabilities", "tenant.provision",
    )
    assert detail["consequence"] == CONSEQUENCE
    assert (detail["organization_count"], detail["organization_slugs"], detail["hidden_organization_count"]) == (
        2, ["acme-cloud"], 1,
    )
    assert "fossgo" not in response.text, "a partner was told the slug of an organization it cannot see"
    assert detail["message"] == (
        "This key owns 2 OrcaHub organizations (acme-cloud and 1 you cannot see). Withdrawing its "
        "tenant.provision capability stops provisioning buyer dashboard access for them: an order completed "
        "in OrcaHub no longer gives its buyer access here. Their sync and member updates continue. Confirm "
        "to withdraw it anyway."
    )
    assert "force" in detail["proceed"]
    assert owner["vault"].writes == [] and owner["evicted"] == [] and owner["log"].calls == []
    assert stored_capabilities(owner) == FULL


@pytest.mark.parametrize("who", ["admin", "partner"])
async def test_with_force_it_goes_through_and_is_recorded_as_forced(owner: Dict[str, Any], who: str) -> None:
    user = ADMIN if who == "admin" else PARTNER
    owner["scope"](user)
    response = await send(user, owner["db"], "PUT", ROUTE, {"capabilities": WITHOUT_PROVISIONING, "force": True})
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"] == WITHOUT_PROVISIONING
    assert stored_capabilities(owner) == WITHOUT_PROVISIONING
    assert owner["evicted"] == [PLAIN]
    (record,) = owner["log"].calls
    assert (record["action"], record["result"]) == ("integration.api_key.capabilities.update", "success")
    details = record["details"]
    assert details["forced"] is True and details["capabilities_withdrawn"] == ["tenant.provision"]
    ids = owner["ids"]
    expected = sorted([ids["acme-cloud"], ids["fossgo"]]) if who == "admin" else [ids["acme-cloud"]]
    assert sorted(details["owned_organization_ids"]) == expected
    assert (details["owned_organization_count"], details["owned_organization_hidden_count"]) == (
        2, 0 if who == "admin" else 1,
    )


async def test_force_does_not_lift_the_sync_refusal(owner: Dict[str, Any]) -> None:
    owner["scope"](ADMIN)
    response = await send(ADMIN, owner["db"], "PUT", ROUTE, {"capabilities": ["inventory.read"], "force": True})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "key_owns_organizations"
    assert owner["vault"].writes == [] and stored_capabilities(owner) == FULL


async def test_a_key_that_owns_nothing_loses_it_without_being_asked(owner: Dict[str, Any]) -> None:
    owner["db"].sync.query(Organization).delete()
    owner["db"].sync.commit()
    owner["scope"](PARTNER)
    response = await send(PARTNER, owner["db"], "PUT", ROUTE, {"capabilities": WITHOUT_PROVISIONING})
    assert response.status_code == 200, response.text
    (record,) = owner["log"].calls
    assert "forced" not in record["details"]


async def test_a_tenant_is_still_refused_with_force(owner: Dict[str, Any]) -> None:
    owner["scope"](TENANT)
    response = await send(TENANT, owner["db"], "PUT", ROUTE, {"capabilities": WITHOUT_PROVISIONING, "force": True})
    assert response.status_code == 403, response.text
    assert owner["vault"].writes == [] and stored_capabilities(owner) == FULL


async def test_another_party_is_still_refused_with_force(owner: Dict[str, Any]) -> None:
    """A partner whose clusters the key does not reach cannot see the key at all."""
    owner["scope"](PARTNER, {"nuc2"})
    response = await send(PARTNER, owner["db"], "PUT", ROUTE, {"capabilities": WITHOUT_PROVISIONING, "force": True})
    assert (response.status_code, response.json()["detail"]) == (404, "API key not found")
    assert owner["vault"].writes == [] and stored_capabilities(owner) == FULL


async def test_ownership_that_cannot_be_read_withdraws_nothing(owner: Dict[str, Any]) -> None:
    owner["scope"](ADMIN)
    response = await send(ADMIN, _Down(), "PUT", ROUTE, {"capabilities": WITHOUT_PROVISIONING, "force": True})
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert owner["vault"].writes == [] and stored_capabilities(owner) == FULL
