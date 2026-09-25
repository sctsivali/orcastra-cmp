"""A push rebinds its creator's personal clusters only into the one org its key claimed.

The creator rule decided who triggers a rebind, not where the clusters go. The destination was
the creator's primary org from org_scope, which puts any org where they are a partner first and
then sorts by id. So another key could push the creator as a partner into its own older org, and
the creator's own onboarding push later moved their clusters there. The destination is now an org
this key claimed (organizations.api_key_id) that this push names with the creator as a partner.
The key must claim exactly one org after the push, so a key that also claims a buyer org binds
nothing (test_a_push_rebinds_only_while_its_key_claims_one_org).
"""

import inspect
import logging
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import select

from app.core import org_scope
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.routers import integrations
from app.routers.integrations import SyncDataRequest, SyncMember
from app.services import authentik_sync, integration_cluster_rebind, shared_cluster_audit
from app.services.authentik_role_reading import RoleReading, known_roles, unknown_role
from app.services.integration_cluster_rebind import REBIND_ACTION, claimed_destinations
from app.services.integration_roles import ROLE_ADMIN, ROLE_PARTNER, stored_member_role
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import _RecordingLogger

K_ATTACKER, K_PARTNER = "oak_attacker", "oak_partner"
ATTACKER = "attacker@example.test"
PARTNER = "partner@example.test"
BUYER = "buyer@example.test"
SECRETS = ("oas_do_not_log_me", "hash-do-not-log")


class Directory:
    """Authentik's role groups per login. Every read and every change is recorded and applied."""

    def __init__(self, groups: Optional[Dict[str, Optional[set]]] = None) -> None:
        self.groups: Dict[str, Optional[set]] = dict(groups or {})
        self.reads: List[str] = []
        self.changes: List[tuple] = []

    async def get_user_role_groups(self, email: str) -> Optional[set]:
        self.reads.append(email)
        held = self.groups.get(email, set())
        return None if held is None else set(held)

    async def read_current_roles(self, email: str) -> RoleReading:
        """The strict read, resolved through the real resolver. None stands for any unknown."""
        self.reads.append(email)
        held = self.groups.get(email, set())
        return unknown_role("authentik_unreachable") if held is None else known_roles(held)

    async def change_integration_role_groups(
        self, email: str, add: Optional[str] = None, remove: Any = frozenset()
    ) -> bool:
        named = frozenset(remove) | ({add} if add else frozenset())
        if named - authentik_sync.INTEGRATION_ROLE_GROUPS:
            raise ValueError(f"an integration may not change {sorted(named)}")
        self.changes.append((email, add, frozenset(remove)))
        held = self.groups.setdefault(email, set())
        if held is not None:
            held -= set(remove)
            if add:
                held.add(add)
        return True


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """The rebind's side effects, recorded: the share audit, the audit record, Authentik."""
    holder: Dict[str, Any] = {"log": _RecordingLogger(), "shares": [], "fail": set()}
    holder["directory"] = Directory({PARTNER: {ROLE_PARTNER}, ATTACKER: {ROLE_PARTNER}})
    monkeypatch.setattr(integration_cluster_rebind, "activity_logger", holder["log"])
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: holder["directory"])

    async def _share_audit(_db: Any, cluster_id: str, **kwargs: Any) -> None:
        holder["shares"].append((cluster_id, kwargs.get("prospective_organization_id")))
        if cluster_id in holder["fail"]:
            raise RuntimeError(f"share audit refused {cluster_id}")

    monkeypatch.setattr(shared_cluster_audit, "assert_cluster_safe_to_share", _share_audit)
    return holder


def key(key_id: str, created_by: Optional[str], **extra: Any) -> Dict[str, Any]:
    return {
        "id": key_id, "app_name": "OrcaHub", "app_type": "orcahub",
        "capabilities": ["inventory.read", "org.sync", "tenant.provision"],
        "cluster_permissions": [], "created_by": created_by,
        "raw": SECRETS[0], "hash": SECRETS[1], **extra,
    }


async def bound(db: AsyncSessionFacade) -> Dict[str, Optional[int]]:
    rows = (await db.execute(select(OrganizationCluster))).scalars().all()
    return {row.cluster_id: row.organization_id for row in rows}


async def org_id(db: AsyncSessionFacade, slug: str) -> int:
    return (await db.execute(select(Organization.id).where(Organization.slug == slug))).scalar_one()


async def push(db: AsyncSessionFacade, api_key: Dict[str, Any], body: Dict[str, Any]) -> Any:
    return await integrations.sync_data(SyncDataRequest.model_validate(body), api_key, None, db)


def body(orgs: List[Dict[str, Any]], members: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The body OrcaHub v1.32.0 sends (orcastra.client.js sync). app_url is absent when unset."""
    return {"include_clusters": True, "include_projects": True, "organizations": orgs, "members": members}


def org(ext: str, slug: str) -> Dict[str, Any]:
    return {"id": ext, "name": slug.title(), "slug": slug, "description": None}


def member(email: str, ext: str, role: str) -> Dict[str, Any]:
    return {"email": email, "name": email.split("@")[0], "organization_id": ext, "role": role}


async def personal_cluster(db: AsyncSessionFacade, cluster_id: str, owner: str) -> None:
    db.add(OrganizationCluster(organization_id=None, cluster_id=cluster_id, registered_by=owner))
    await db.commit()


# ── the probe: another key chose the destination ───────────────────────────────────────────────

async def test_another_key_cannot_choose_where_the_creators_own_push_rebinds(world: Dict[str, Any]) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)

    # another key pushes the partner as PARTNER into its own org, which is older (lower id)
    await push(db, key(K_ATTACKER, ATTACKER),
               body([org("ext-a", "attacker")], [member(PARTNER, "ext-a", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] is None
    assert await org_scope._ordered_org_ids_for_email(PARTNER, db) == [await org_id(db, "attacker")]

    # the partner's own OrcaHub pushes the partner into the partner's org
    await push(db, key(K_PARTNER, PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    own = await org_id(db, "partner")
    assert (await bound(db))["partner-cluster"] == own
    assert world["shares"] == [("partner-cluster", own)]
    (call,) = world["log"].calls
    assert call["action"] == REBIND_ACTION
    assert call["details"]["to_organization_id"] == own and call["details"]["api_key_id"] == K_PARTNER


async def test_orcahubs_onboarding_push_of_the_partner_org_alone_binds_into_it(world: Dict[str, Any]) -> None:
    """The partner's own org and its members, as OrcaHub sends it before the partner has a buyer."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    onboarding = body([org("cm-partner", "partner")], [member(PARTNER, "cm-partner", "PARTNER")])
    result = await push(db, key(K_PARTNER, PARTNER), onboarding)
    assert (result.organizations_synced, result.members_synced) == (1, 1)
    own = await org_id(db, "partner")
    assert (await bound(db))["partner-cluster"] == own
    assert world["shares"] == [("partner-cluster", own)]
    assert [c["details"]["to_organization_id"] for c in world["log"].calls] == [own]

    await push(db, key(K_PARTNER, PARTNER), onboarding)
    assert len(world["log"].calls) == 1, "the scheduled re-push moves nothing"


async def test_orcahubs_push_of_the_partner_org_and_a_buyer_org_binds_nothing(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The same key claims the buyer org, so it claims two orgs and neither is known to be the partner's."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    await personal_cluster(db, "buyer-cluster", BUYER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    result = await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-partner", "partner"), org("cm-buyer", "buyer")],
        [member(PARTNER, "cm-partner", "PARTNER"), member(BUYER, "cm-buyer", "USER")],
    ))
    assert (result.organizations_synced, result.members_synced) == (2, 2)
    assert await bound(db) == {"partner-cluster": None, "buyer-cluster": None}
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = [r.getMessage() for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_PARTNER in warning and " 2 organizations" in warning and "@" not in warning


async def test_the_primary_org_ordering_is_not_consulted(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The partner owns an older dashboard org, which the ordering would pick. The push does not."""
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Local", slug="local", source="local", created_by=PARTNER, is_active=True))
    await db.commit()
    await personal_cluster(db, "partner-cluster", PARTNER)
    asked: List[str] = []
    real = org_scope._ordered_org_ids_for_email

    async def _ordering(email: str, session: Any) -> List[int]:
        asked.append(email)
        return await real(email, session)

    monkeypatch.setattr(org_scope, "_ordered_org_ids_for_email", _ordering)
    await push(db, key(K_PARTNER, PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] == await org_id(db, "partner")
    assert await real(PARTNER, db) == [await org_id(db, "local"), await org_id(db, "partner")]
    assert asked == []


# ── zero or several destinations ───────────────────────────────────────────────────────────────

async def test_several_claimed_orgs_naming_the_creator_rebind_nothing(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("ext-p", "partner"), org("ext-q", "second")],
        [member(PARTNER, "ext-p", "PARTNER"), member(PARTNER, "ext-q", "PARTNER")],
    ))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = [r for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_PARTNER in warning.getMessage() and " 2 organizations" in warning.getMessage()
    assert not any(secret in caplog.text for secret in SECRETS)


async def test_zero_claimed_orgs_naming_the_creator_rebind_nothing(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """The creator is a member of an org another key claimed, and this push names it."""
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Theirs", slug="theirs", source="orcahub", external_id="ext-t",
                        api_key_id=K_ATTACKER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"))
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)

    moved = await integration_cluster_rebind.rebind_creator_clusters(
        db, PARTNER, [SyncMember(**member(PARTNER, "ext-t", "PARTNER"))],
        api_key_id=K_PARTNER, creator=PARTNER, roles=world["directory"],
    )
    assert moved == 0 and (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = [r for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_PARTNER in warning.getMessage() and "it claims 0 organizations" in warning.getMessage()


async def test_nothing_to_move_asks_for_no_destination(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A creator with no personal cluster: the scheduled push logs nothing about a rebind."""
    db = sqlite_session(*org_tables())
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("ext-p", "partner"), org("ext-q", "second")],
        [member(PARTNER, "ext-p", "PARTNER"), member(PARTNER, "ext-q", "PARTNER")],
    ))
    assert [r for r in caplog.records if r.name == integration_cluster_rebind.__name__] == []


async def test_a_remove_in_the_same_push_is_not_a_destination(world: Dict[str, Any]) -> None:
    """The key's only org names the creator a partner and then removes them, so no row reads back."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("ext-p", "partner")],
        [member(PARTNER, "ext-p", "PARTNER"), {**member(PARTNER, "ext-p", "PARTNER"), "action": "remove"}],
    ))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []


async def test_an_inactive_claimed_org_is_not_a_destination(world: Dict[str, Any]) -> None:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Asleep", slug="asleep", source="orcahub", external_id="ext-z",
                        api_key_id=K_PARTNER, created_by="system", is_active=False))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"))
    await db.commit()
    assert await claimed_destinations(
        db, [SyncMember(**member(PARTNER, "ext-z", "PARTNER"))], email=PARTNER, api_key_id=K_PARTNER
    ) == []


async def test_a_named_org_needs_the_membership_row(world: Dict[str, Any]) -> None:
    """Named in the push, but the member write did not land: not a destination."""
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Mine", slug="mine", source="orcahub", external_id="ext-m",
                        api_key_id=K_PARTNER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=BUYER, role="tenant"))
    await db.commit()
    assert await claimed_destinations(
        db, [SyncMember(**member(PARTNER, "ext-m", "PARTNER"))], email=PARTNER, api_key_id=K_PARTNER
    ) == []


async def test_a_remove_names_no_destination(world: Dict[str, Any]) -> None:
    """A remove the member loop could not apply leaves the row, and still names no destination."""
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Mine", slug="mine", source="orcahub", external_id="ext-m",
                        api_key_id=K_PARTNER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"))
    await db.commit()
    removal = SyncMember(**{**member(PARTNER, "ext-m", "PARTNER"), "action": "remove"})
    assert await claimed_destinations(db, [removal], email=PARTNER, api_key_id=K_PARTNER) == []
    upsert = SyncMember(**member(PARTNER, "ext-m", "PARTNER"))
    assert await claimed_destinations(db, [upsert], email=PARTNER, api_key_id=K_PARTNER) == [1]


@pytest.mark.parametrize("api_key_id", ["", K_ATTACKER])
async def test_only_this_keys_claim_counts(world: Dict[str, Any], api_key_id: str) -> None:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Unclaimed", slug="unclaimed", source="orcahub", external_id="ext-u",
                        api_key_id=None, created_by="system", is_active=True))
    db.add(Organization(name="Mine", slug="mine", source="orcahub", external_id="ext-m",
                        api_key_id=K_PARTNER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"))
    db.add(OrganizationMember(organization_id=2, user_email=PARTNER, role="partner"))
    await db.commit()
    members = [SyncMember(**member(PARTNER, "ext-u", "PARTNER")), SyncMember(**member(PARTNER, "ext-m", "PARTNER"))]
    assert await claimed_destinations(db, members, email=PARTNER, api_key_id=api_key_id) == []
    assert await claimed_destinations(db, members, email=PARTNER, api_key_id=K_PARTNER) == [2]


def test_the_directory_fake_refuses_role_admin_like_the_real_writer() -> None:
    """The fake applies changes, so it must refuse what the real Authentik writer refuses."""
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(Directory().change_integration_role_groups(PARTNER, add=ROLE_ADMIN))


# ── the creator must be a partner in the destination ───────────────────────────────────────────

@pytest.mark.parametrize("role", ["PARTNER", "USER", "TENANT", "user", "OWNER", " PARTNER"])
async def test_a_buyer_org_naming_the_creator_with_any_role_binds_nothing(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture, role: str
) -> None:
    """A buyer org's own partner can add the seller's creator with any role, PARTNER included, and
    the seller's push carries every member of every buyer org. With the creator absent from the
    seller's roster the buyer org was the one destination. The key claims both orgs now, so nothing
    binds whatever role the buyer org gave."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-partner", "partner"), org("cm-buyer", "buyer")],
        [member(BUYER, "cm-partner", "PARTNER"), member(PARTNER, "cm-buyer", role),
         member(BUYER, "cm-buyer", "USER")],
    ))
    stored = (await db.execute(select(OrganizationMember.role).where(
        OrganizationMember.user_email == PARTNER))).scalars().all()
    assert stored == [stored_member_role(role)]
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = [r.getMessage() for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_PARTNER in warning and " 2 organizations" in warning and "@" not in warning


@pytest.mark.parametrize("role", ["USER", "PARTNER"])
async def test_a_partner_in_its_own_org_and_in_a_buyer_org_binds_nothing(
    world: Dict[str, Any], role: str,
) -> None:
    """The creator is a partner in the key's own org. The buyer org names them too and is claimed by
    the same key, so the key claims two orgs and nothing binds, whichever role the buyer org gave."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    await push(db, key(K_PARTNER, PARTNER), body(
        [org("cm-partner", "partner"), org("cm-buyer", "buyer")],
        [member(PARTNER, "cm-partner", "PARTNER"), member(PARTNER, "cm-buyer", role),
         member(BUYER, "cm-buyer", "USER")],
    ))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []


@pytest.mark.parametrize("role", ["PARTNER", "partner", "Partner"])
async def test_the_pushed_role_is_read_the_way_the_member_sync_stores_it(
    world: Dict[str, Any], role: str
) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    await push(db, key(K_PARTNER, PARTNER), body([org("ext-p", "partner")], [member(PARTNER, "ext-p", role)]))
    assert (await db.execute(select(OrganizationMember.role))).scalars().all() == ["partner"]
    assert (await bound(db))["partner-cluster"] == await org_id(db, "partner")


async def test_a_stale_partner_row_is_no_destination_when_this_push_says_user(world: Dict[str, Any]) -> None:
    """The row still says partner from an earlier push (this push's upsert did not land)."""
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Mine", slug="mine", source="orcahub", external_id="ext-m",
                        api_key_id=K_PARTNER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"))
    await db.commit()
    assert await claimed_destinations(
        db, [SyncMember(**member(PARTNER, "ext-m", "USER"))], email=PARTNER, api_key_id=K_PARTNER
    ) == []
    assert await claimed_destinations(
        db, [SyncMember(**member(PARTNER, "ext-m", "PARTNER"))], email=PARTNER, api_key_id=K_PARTNER
    ) == [1]


async def test_a_pushed_partner_whose_row_says_tenant_is_no_destination(world: Dict[str, Any]) -> None:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Mine", slug="mine", source="orcahub", external_id="ext-m",
                        api_key_id=K_PARTNER, created_by="system", is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email=PARTNER, role="tenant"))
    await db.commit()
    assert await claimed_destinations(
        db, [SyncMember(**member(PARTNER, "ext-m", "PARTNER"))], email=PARTNER, api_key_id=K_PARTNER
    ) == []


def test_the_member_sync_and_the_destination_read_one_role_mapping() -> None:
    """Two copies of the mapping could drift, and then the destination would disagree with the row."""
    assert not hasattr(integrations, "_ORCAHUB_ROLE_MAP")
    assert "stored_member_role(m.role)" in inspect.getsource(integrations._upsert_synced_members)
    assert "stored_member_role(" in inspect.getsource(claimed_destinations)
    assert [stored_member_role(r) for r in ("PARTNER", "partner", "USER", "TENANT", "OWNER", "")] == [
        "partner", "partner", "tenant", "tenant", "tenant", "tenant",
    ]
