"""OrcaHub hears about a project only when the organization it serves reaches that project.

OrcaHub events fanned out to every OrcaHub organization bound to the event's cluster, so on a
cluster two parties hold, each one's OrcaHub heard, and could bill, the other's projects (pre-1.0.0
review, 2026-11-06). An organization hears an event when no other party holds the cluster for it,
or when the project is in its bound there (`default` and what its party's policies name); a delete
reaches the organizations that held what it removed (services/orcahub_reach.py).
"""

from typing import Any, Dict, List

import pytest

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    OrganizationOwnerKey,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.services import orcahub_reach, orcahub_webhook
from tests._sqlite_session import sqlite_session, with_json_tables

SELLER_KEY = "oak_seller"
OTHER_KEY = "oak_other"
OWN, MIXED = "own-cluster", "mixed-cluster"


async def _world(*, unbound_on_own: bool = False):
    """A seller (1) and its buyer (2) on their own cluster; the seller beside another party (3)."""
    db = with_json_tables(
        sqlite_session(Organization, OrganizationMember, OrganizationCluster, OrganizationOwnerKey),
        TenantAccessPolicy, TenantPolicyRule,
    )
    db.add_all([
        Organization(id=1, name="seller", slug="seller", created_by="system", is_active=True, source="orcahub", api_key_id=SELLER_KEY),
        Organization(id=2, name="buyer", slug="buyer", created_by="system", is_active=True, source="orcahub", api_key_id=SELLER_KEY),
        Organization(id=3, name="other", slug="other", created_by="system", is_active=True, source="orcahub", api_key_id=OTHER_KEY),
    ])
    await db.commit()
    db.add_all([
        OrganizationCluster(organization_id=1, cluster_id=OWN),
        OrganizationCluster(organization_id=2, cluster_id=OWN),
        OrganizationCluster(organization_id=1, cluster_id=MIXED),
        OrganizationCluster(organization_id=3, cluster_id=MIXED),
        TenantAccessPolicy(id=10, organization_id=1, name="seller", created_by="system"),
        TenantAccessPolicy(id=30, organization_id=3, name="other", created_by="system"),
        TenantPolicyRule(policy_id=10, cluster_id=MIXED, project_name="sellers"),
        TenantPolicyRule(policy_id=30, cluster_id=MIXED, project_name="theirs"),
    ])
    if unbound_on_own:
        db.add(OrganizationCluster(organization_id=None, cluster_id=OWN, registered_by="someone@example.test"))
    await db.commit()
    return db


async def _orgs(db, *ids):
    from sqlalchemy import select

    return list((await db.execute(select(Organization).where(Organization.id.in_(ids)).order_by(Organization.id))).scalars().all())


async def _reaching(db, cluster, project, ids, **kwargs) -> List[int]:
    return [o.id for o in await orcahub_reach.organizations_reaching(db, cluster, project, await _orgs(db, *ids), **kwargs)]


async def test_on_its_own_sides_cluster_an_organization_hears_every_project() -> None:
    db = await _world()
    assert await _reaching(db, OWN, "anything", (1, 2)) == [1, 2], "a seller and the buyer it provisioned are one side"


async def test_beside_another_party_an_organization_hears_only_its_bound() -> None:
    db = await _world()
    assert await _reaching(db, MIXED, "sellers", (1, 3)) == [1]
    assert await _reaching(db, MIXED, "theirs", (1, 3)) == [3]
    assert await _reaching(db, MIXED, "default", (1, 3)) == [1, 3], "default is in every party's bound"
    assert await _reaching(db, MIXED, "nobodys", (1, 3)) == []


async def test_a_star_rule_reaches_every_project() -> None:
    db = await _world()
    db.add(TenantPolicyRule(policy_id=10, cluster_id=MIXED, project_name="*"))
    await db.commit()
    assert await _reaching(db, MIXED, "theirs", (1,)) == [1]


async def test_a_delete_reaches_the_organizations_that_held_the_project() -> None:
    db = await _world()
    assert await _reaching(db, MIXED, "gone", (1, 3), held_by={3}, deleted=True) == [3]
    assert await _reaching(db, MIXED, "gone", (1, 3), held_by={2}, deleted=True) == [1], (
        "a buyer's policy held it, and the seller that provisioned the buyer is on its side"
    )


async def test_a_party_that_reuses_a_deleted_name_is_not_told_it_went_away() -> None:
    db = await _world()
    # Organization 1 created its own "theirs" while organization 3's delete was being announced.
    db.add(TenantPolicyRule(policy_id=10, cluster_id=MIXED, project_name="theirs"))
    await db.commit()
    assert await _reaching(db, MIXED, "theirs", (1, 3), held_by={3}, deleted=True) == [3]


async def test_a_registration_not_yet_bound_counts_as_another_holder() -> None:
    db = await _world(unbound_on_own=True)
    assert await _reaching(db, OWN, "anything", (1,)) == []
    assert await _reaching(db, OWN, "default", (1,)) == [1]


def _capture(monkeypatch) -> List[Dict[str, Any]]:
    sent: List[Dict[str, Any]] = []

    async def post(creds, payload):
        sent.append({"org": creds["org"], **payload})

    async def creds(org, _owners):
        return {"org": org.id}

    async def no_owners(_db, _orgs):
        return {}

    monkeypatch.setattr(orcahub_webhook, "_post_signed", post)
    monkeypatch.setattr(orcahub_webhook, "_get_orcahub_credentials", creds)
    monkeypatch.setattr(orcahub_webhook, "_owner_sets", no_owners)
    return sent


class _Maker:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_a):
        return False


async def test_the_fire_and_forget_emitter_sends_only_to_organizations_that_reach_it(monkeypatch) -> None:
    db = await _world()
    sent = _capture(monkeypatch)
    monkeypatch.setattr(orcahub_webhook, "async_session_maker", _Maker(db))
    await orcahub_webhook._emit("instance.created", MIXED, "theirs", "vm-a", None, "INSTANCE")
    assert [s["org"] for s in sent] == [3]


async def test_an_unreadable_reach_sends_nothing(monkeypatch) -> None:
    db = await _world()
    sent = _capture(monkeypatch)
    monkeypatch.setattr(orcahub_webhook, "async_session_maker", _Maker(db))

    async def broken(*_a, **_k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(orcahub_webhook, "organizations_reaching", broken)
    await orcahub_webhook._emit("instance.created", MIXED, "default", "vm-a", None, "INSTANCE")
    assert sent == []
    with pytest.raises(RuntimeError):
        await orcahub_webhook.emit_orcahub_event_durable(
            event_id="e1", event_type="instance.created", cluster_id=MIXED, project="default", name="vm-a"
        )


async def test_a_durable_delete_reaches_its_holders(monkeypatch) -> None:
    db = await _world()
    sent = _capture(monkeypatch)
    monkeypatch.setattr(orcahub_webhook, "async_session_maker", _Maker(db))
    delivered = await orcahub_webhook.emit_orcahub_event_durable(
        event_id="e2", event_type="project.deleted", cluster_id=MIXED, project="gone", name="gone",
        resource_type="PROJECT", namespace="orcastra-project-delete", reached_organizations={3},
    )
    assert delivered == 1 and [s["org"] for s in sent] == [3]


async def test_a_migration_billing_event_names_the_jobs_owner(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services.migrations import completion

    reached: Dict[str, Any] = {}

    async def durable(**kwargs):
        reached.update(kwargs)
        return 1

    monkeypatch.setattr(completion, "emit_orcahub_event_durable", durable)
    row = SimpleNamespace(channel="billing", event_type="instance.deleted", event_id="evt-1", payload={
        "cluster_id": MIXED, "project": "gone", "resource_name": "vm", "occurred_at": "2026-11-06T00:00:00Z",
        "held_organizations": [7, True, "8"],
    })
    assert await completion.deliver_completion_event(row) is True
    assert reached["reached_organizations"] == [7], "only integer organization ids are holders"
