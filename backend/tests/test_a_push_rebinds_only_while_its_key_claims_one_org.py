"""A push rebinds its creator's personal clusters only while its key claims exactly one org.

A seller's key claims every buyer org it syncs or provisions, and a buyer org's own partner can add
the seller's creator as PARTNER. The pushed role cannot tell the seller's org from a buyer's, so a
buyer org could be the one destination, or a second one that blocked the seller's own. The key's
claims are counted after the push, active or not, and anything but one rebinds nothing. The warning
names the key id and the count, never an email.
"""

import logging
from typing import Any, Dict, List

import pytest

from app.models.db_models import Organization, OrganizationMember
from app.services import integration_cluster_rebind
from app.services.integration_cluster_rebind import claimed_organization_count
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    BUYER,
    K_ATTACKER,
    K_PARTNER,
    PARTNER,
    body,
    bound,
    key,
    member,
    org,
    org_id,
    personal_cluster,
    push,
    world,  # noqa: F401
)

COPARTNER = "copartner@example.test"


async def claim(db: AsyncSessionFacade, ext: str, slug: str, owner: Any = K_PARTNER, active: bool = True,
                source: str = "orcahub") -> int:
    db.add(Organization(name=slug.title(), slug=slug, source=source, external_id=ext, api_key_id=owner,
                        created_by="system", is_active=active))
    await db.commit()
    return await org_id(db, slug)


def rebind_warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.name == integration_cluster_rebind.__name__]


def assert_warned(caplog: pytest.LogCaptureFixture, count: int) -> None:
    (warning,) = rebind_warnings(caplog)
    assert K_PARTNER in warning and f"it claims {count} organizations" in warning
    assert "@" not in warning


async def test_the_count_takes_every_org_the_key_claimed_active_or_not() -> None:
    db = sqlite_session(*org_tables())
    await claim(db, "ext-p", "partner")
    await claim(db, "ext-z", "asleep", active=False)
    await claim(db, "ext-l", "local", source="local")
    await claim(db, "ext-a", "theirs", owner=K_ATTACKER)
    await claim(db, "ext-u", "unclaimed", owner=None)
    await claim(db, "ext-e", "empty", owner="")
    assert await claimed_organization_count(db, K_PARTNER) == 3
    assert await claimed_organization_count(db, K_ATTACKER) == 1
    assert await claimed_organization_count(db, "") == 0
    assert await claimed_organization_count(db, "oak_nobody") == 0


async def test_a_buyer_org_claimed_by_an_earlier_push_still_counts(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """The buyer org is absent from this push, but the key still holds it."""
    db = sqlite_session(*org_tables())
    await push(db, key(K_PARTNER, PARTNER), body([org("cm-buyer", "buyer")], [member(BUYER, "cm-buyer", "PARTNER")]))
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER),
               body([org("cm-partner", "partner")], [member(PARTNER, "cm-partner", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    assert_warned(caplog, 2)


async def test_an_inactive_claimed_org_counts(world: Dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await claim(db, "cm-old", "old", active=False)
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER),
               body([org("cm-partner", "partner")], [member(PARTNER, "cm-partner", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    assert_warned(caplog, 2)


async def test_an_inactive_own_org_does_not_leave_a_buyer_org_the_only_one(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """Counting only active orgs would make the buyer org the key's one org and the destination."""
    db = sqlite_session(*org_tables())
    own = await claim(db, "cm-partner", "partner", active=False)
    db.add(OrganizationMember(organization_id=own, user_email=PARTNER, role="partner"))
    await db.commit()
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-buyer", "buyer")], [member(BUYER, "cm-buyer", "PARTNER"), member(PARTNER, "cm-buyer", "PARTNER")],
    ))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    assert_warned(caplog, 2)


async def test_orgs_of_another_key_and_unclaimed_orgs_do_not_count(world: Dict[str, Any]) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    theirs = await claim(db, "ext-a", "theirs", owner=K_ATTACKER)
    unclaimed = await claim(db, "ext-u", "unclaimed", owner=None)
    db.add(OrganizationMember(organization_id=theirs, user_email=PARTNER, role="partner"))
    db.add(OrganizationMember(organization_id=unclaimed, user_email=PARTNER, role="partner"))
    await db.commit()
    await personal_cluster(db, "partner-cluster", PARTNER)
    await push(db, key(K_PARTNER, PARTNER),
               body([org("cm-partner", "partner")], [member(PARTNER, "cm-partner", "PARTNER")]))
    own = await org_id(db, "partner")
    assert (await bound(db))["partner-cluster"] == own
    assert [c["details"]["to_organization_id"] for c in world["log"].calls] == [own]


async def test_a_cluster_bound_earlier_stays_when_a_buyer_org_is_claimed_later(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """Nothing is unbound. A cluster registered after the first sale waits for the dashboard."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "first-cluster", PARTNER)
    await push(db, key(K_PARTNER, PARTNER),
               body([org("cm-partner", "partner")], [member(PARTNER, "cm-partner", "PARTNER")]))
    own = await org_id(db, "partner")
    assert (await bound(db))["first-cluster"] == own

    await personal_cluster(db, "later-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-partner", "partner"), org("cm-buyer", "buyer")],
        [member(PARTNER, "cm-partner", "PARTNER"), member(BUYER, "cm-buyer", "USER")],
    ))
    assert await bound(db) == {"first-cluster": own, "later-cluster": None}
    assert len(world["log"].calls) == 1
    assert_warned(caplog, 2)


async def test_the_count_is_read_before_authentik_is_asked(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    strict: List[str] = []
    lenient = world["directory"].read_current_roles

    async def _strict(email: str) -> Any:
        strict.append(email)
        return await lenient(email)

    monkeypatch.setattr(world["directory"], "read_current_roles", _strict)
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-partner", "partner"), org("cm-buyer", "buyer")],
        [member(PARTNER, "cm-partner", "PARTNER"), member(PARTNER, "cm-buyer", "PARTNER")],
    ))
    assert strict == [] and (await bound(db))["partner-cluster"] is None
    assert_warned(caplog, 2)


@pytest.mark.parametrize("role", ["USER", "TENANT", "user", "OWNER", " PARTNER"])
async def test_a_creator_below_partner_in_the_keys_only_org_is_no_destination(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture, role: str,  # noqa: F811
) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-partner", "partner")], [member(COPARTNER, "cm-partner", "PARTNER"), member(PARTNER, "cm-partner", role)],
    ))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = rebind_warnings(caplog)
    assert K_PARTNER in warning and " 0 organizations it claimed name its creator" in warning
