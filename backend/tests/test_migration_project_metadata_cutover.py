"""A relocated project's own records follow it to its new identity, and back.

note: as in test_migration_metadata_cutover.py, the tables are created from a copy of their
metadata with JSON in place of JSONB. The statements under test run on the production models.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import JSON, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import (
    Organization,
    OrganizationMember,
    Tag,
    TagAssignment,
    TagCategory,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.services.migrations.metadata_cutover import InstanceIdentity
from app.services.migrations.project_metadata_cutover import ProjectIdentity, move_project_records
from app.services.project_grants import managed_policy_name
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

ORG, OTHER_ORG = 1, 2
TEAM, MANAGED_A, ORCAHUB, FOREIGN = 1, 2, 3, 4
OLD = ProjectIdentity("cluster-a", "web")
NEW = ProjectIdentity("cluster-b", "web")
PAIRS = [
    (
        InstanceIdentity("cluster-a", "web", "web-1", uuid="uuid-old"),
        InstanceIdentity("cluster-b", "web", "web-1", uuid="uuid-new"),
    )
]


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    models = (
        Organization,
        OrganizationMember,
        TagCategory,
        Tag,
        TagAssignment,
        TenantAccessPolicy,
        TenantPolicyRule,
        TenantPolicyAssignment,
        SnapshotSchedule,
    )
    for model in models:
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(facade.sync.get_bind())
    _seed(facade, Organization, *(
        {"id": org, "name": f"Org {org}", "slug": f"org-{org}", "created_by": "ops@example.test"}
        for org in (ORG, OTHER_ORG)
    ))
    _seed(facade, Tag, {"id": 1, "name": "prod"})
    _seed(facade, TenantAccessPolicy, *(
        {"id": policy, "organization_id": org, "name": name, "source": source, "created_by": "ops@example.test"}
        for policy, org, name, source in (
            (TEAM, ORG, "team", "local"),
            (MANAGED_A, ORG, managed_policy_name("cluster-a"), "local"),
            (ORCAHUB, ORG, "synced", "orcahub"),
            (FOREIGN, OTHER_ORG, "theirs", "local"),
        )
    ))
    _seed(facade, TenantPolicyRule, *(
        {"policy_id": policy, "cluster_id": "cluster-a", "project_name": project, "instance_name": instance, "permissions": perms}
        for policy, project, instance, perms in (
            (TEAM, "web", "*", ["view", "operate"]),
            (TEAM, "web", "web-1", ["console"]),
            (MANAGED_A, "web", "*", ["view"]),
            (MANAGED_A, "other", "*", ["view"]),
            (ORCAHUB, "web", "*", ["view"]),
            (FOREIGN, "web", "*", ["view"]),
            (TEAM, "*", "*", ["view"]),
        )
    ))
    _seed(facade, TagAssignment, {
        "tag_id": 1, "resource_type": "project", "resource_id": "web", "resource_host": "cluster-a",
        "resource_project": None,
    })
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


def _seed(db: AsyncSessionFacade, model: Any, *rows: dict[str, Any]) -> None:
    db.sync.execute(insert(model), list(rows))
    db.sync.commit()


def _rules(db: AsyncSessionFacade) -> set[tuple[Any, ...]]:
    rows = db.sync.execute(
        select(
            TenantAccessPolicy.name,
            TenantPolicyRule.cluster_id,
            TenantPolicyRule.project_name,
            TenantPolicyRule.instance_name,
            TenantPolicyRule.permissions,
        ).join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
    ).all()
    return {(name, cluster, project, instance, tuple(perms)) for name, cluster, project, instance, perms in rows}


def _policies(db: AsyncSessionFacade) -> set[str]:
    return set(db.sync.execute(select(TenantAccessPolicy.name)).scalars())


@pytest.mark.asyncio
async def test_project_records_follow_the_relocated_project(db: AsyncSessionFacade) -> None:
    before = _rules(db)
    policies = _policies(db)

    counts = await move_project_records(db, old=OLD, new=NEW, organization_id=ORG, instances=PAIRS)
    db.sync.commit()

    rules = _rules(db)
    assert counts["project_rules"] == 2 and counts["tenant_rules"] == 1 and counts["project_tags"] == 1
    assert ("team", "cluster-b", "web", "*", ("view", "operate")) in rules
    assert ("team", "cluster-b", "web", "web-1", ("console",)) in rules
    # The managed rule stays in its own policy, so the tenants that policy reaches are unchanged.
    assert (managed_policy_name("cluster-a"), "cluster-b", "web", "*", ("view",)) in rules
    assert (managed_policy_name("cluster-a"), "cluster-a", "other", "*", ("view",)) in rules
    assert _policies(db) == policies
    # OrcaHub's, another organization's, and wildcard rules are never moved.
    assert ("synced", "cluster-a", "web", "*", ("view",)) in rules
    assert ("theirs", "cluster-a", "web", "*", ("view",)) in rules
    assert ("team", "cluster-a", "*", "*", ("view",)) in rules
    tag = db.sync.execute(select(TagAssignment.resource_host, TagAssignment.resource_id)).one()
    assert tuple(tag) == ("cluster-b", "web")

    await move_project_records(
        db, old=NEW, new=OLD, organization_id=ORG, instances=[(after, before_) for before_, after in PAIRS]
    )
    db.sync.commit()

    assert _rules(db) == before
    assert _policies(db) == policies


@pytest.mark.asyncio
async def test_a_policy_assigned_to_tenants_keeps_reaching_exactly_them(db: AsyncSessionFacade) -> None:
    """Security review: re-homing a rule into another cluster's managed policy would hand the
    project to that policy's tenants."""
    _seed(db, TenantAccessPolicy, {
        "id": 9, "organization_id": ORG, "name": managed_policy_name("cluster-b"), "source": "local",
        "created_by": "ops@example.test",
    })

    await move_project_records(db, old=OLD, new=NEW, organization_id=ORG, instances=[])
    db.sync.commit()

    holders = db.sync.execute(
        select(TenantPolicyRule.policy_id).where(
            TenantPolicyRule.cluster_id == "cluster-b", TenantPolicyRule.project_name == "web"
        )
    ).scalars().all()
    assert sorted(holders) == sorted([TEAM, MANAGED_A])


@pytest.mark.asyncio
async def test_a_same_cluster_rename_keeps_every_rule_in_its_policy(db: AsyncSessionFacade) -> None:
    renamed = ProjectIdentity("cluster-a", "web-renamed")

    await move_project_records(db, old=OLD, new=renamed, organization_id=ORG, instances=[])
    db.sync.commit()

    rules = _rules(db)
    assert (managed_policy_name("cluster-a"), "cluster-a", "web-renamed", "*", ("view",)) in rules
    assert ("team", "cluster-a", "web-renamed", "*", ("view", "operate")) in rules
