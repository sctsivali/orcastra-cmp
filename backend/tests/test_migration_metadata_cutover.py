"""Per-instance Orcastra records follow a moved instance to its new identity, and back.

tenant_policy_rules.permissions is JSONB, so the tables are created from a copy of their
metadata with JSON in its place (as test_tenant_policy_details_require_own_assignment.py does).
The statements under test still run against the production model metadata.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from sqlalchemy import JSON, MetaData, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import (
    Organization,
    Tag,
    TagAssignment,
    TagCategory,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.services.migrations.metadata_cutover import InstanceIdentity, move_instance_records
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

ORG, OTHER_ORG = 1, 2
LOCAL, ORCAHUB, FOREIGN = 1, 2, 3
OLD = InstanceIdentity("cluster-a", "tenant-a", "web", cluster_fingerprint="fp-a", uuid="uuid-old")
NEW = InstanceIdentity("cluster-b", "tenant-b", "web", cluster_fingerprint="fp-b", uuid="uuid-new")
NOW = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    models = (Organization, TagCategory, Tag, TagAssignment, TenantAccessPolicy, TenantPolicyRule, SnapshotSchedule)
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
    _seed(facade, Tag, *({"id": tag, "name": f"tag-{tag}"} for tag in (1, 2, 3)))
    _seed(facade, TenantAccessPolicy, *(
        {"id": policy, "organization_id": org, "name": f"policy-{policy}", "source": source,
         "created_by": "ops@example.test"}
        for policy, org, source in ((LOCAL, ORG, "local"), (ORCAHUB, ORG, "orcahub"), (FOREIGN, OTHER_ORG, "local"))
    ))
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


def _seed(db: AsyncSessionFacade, model: Any, *rows: dict[str, Any]) -> None:
    db.sync.execute(insert(model), list(rows))
    db.sync.commit()


def _assignment(
    tag_id: int, at: InstanceIdentity, *, kind: str = "instance", name: Optional[str] = None
) -> dict[str, Any]:
    return {
        "tag_id": tag_id, "resource_type": kind, "resource_id": name or at.name,
        "resource_host": at.cluster_id, "resource_project": at.project,
    }


def _schedule(schedule_id: int, org: int, at: InstanceIdentity, *, name: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": schedule_id, "organization_id": org, "cluster_id": at.cluster_id, "project": at.project,
        "instance_name": name or at.name, "instance_uuid": at.uuid,
        "cluster_fingerprint": at.cluster_fingerprint, "label": "nightly", "frequency": "daily",
        "hour_utc": 2, "owner_email": "ops@example.test", "next_run_at": NOW,
    }


def _rule(
    policy_id: int, at: InstanceIdentity, permissions: list[str], *, instance: Optional[str] = None
) -> dict[str, Any]:
    return {
        "policy_id": policy_id, "cluster_id": at.cluster_id, "project_name": at.project,
        "instance_name": instance or at.name, "permissions": permissions,
    }


def _state(db: AsyncSessionFacade) -> dict[str, set[tuple[Any, ...]]]:
    tags = select(
        TagAssignment.tag_id, TagAssignment.resource_type, TagAssignment.resource_host,
        TagAssignment.resource_project, TagAssignment.resource_id,
    )
    schedules = select(
        SnapshotSchedule.id, SnapshotSchedule.cluster_id, SnapshotSchedule.project,
        SnapshotSchedule.instance_name, SnapshotSchedule.instance_uuid, SnapshotSchedule.cluster_fingerprint,
    )
    rules = select(
        TenantPolicyRule.policy_id, TenantPolicyRule.cluster_id, TenantPolicyRule.project_name,
        TenantPolicyRule.instance_name, TenantPolicyRule.permissions,
    )
    return {
        "tags": {tuple(row) for row in db.sync.execute(tags)},
        "schedules": {tuple(row) for row in db.sync.execute(schedules)},
        "rules": {(*row[:4], tuple(row[4])) for row in db.sync.execute(rules)},
    }


async def _move(db: AsyncSessionFacade, old: InstanceIdentity = OLD, new: InstanceIdentity = NEW) -> dict[str, int]:
    return await move_instance_records(db, old=old, new=new, organization_id=ORG)


async def test_instance_tags_follow_the_instance_and_other_resources_stay(db: AsyncSessionFacade) -> None:
    _seed(db, TagAssignment,
          _assignment(1, OLD), _assignment(2, OLD), _assignment(1, OLD, kind="node"),
          _assignment(1, OLD, name="db"), _assignment(2, NEW), _assignment(3, NEW))

    counts = await _move(db)

    assert counts["tags"] == 2
    assert _state(db)["tags"] == {
        (1, "instance", "cluster-b", "tenant-b", "web"),
        (2, "instance", "cluster-b", "tenant-b", "web"),
        (3, "instance", "cluster-b", "tenant-b", "web"),
        (1, "node", "cluster-a", "tenant-a", "web"),
        (1, "instance", "cluster-a", "tenant-a", "db"),
    }


async def test_an_organizations_schedule_moves_with_its_new_identity_pins(db: AsyncSessionFacade) -> None:
    _seed(db, SnapshotSchedule, _schedule(1, ORG, OLD), _schedule(2, OTHER_ORG, NEW), _schedule(3, ORG, OLD, name="db"))

    counts = await _move(db)

    assert counts["schedules"] == 1
    assert _state(db)["schedules"] == {
        (1, "cluster-b", "tenant-b", "web", "uuid-new", "fp-b"),
        (3, "cluster-a", "tenant-a", "db", "uuid-old", "fp-a"),
    }


async def test_another_organizations_schedule_stays_and_nothing_is_cleared_at_the_target(
    db: AsyncSessionFacade,
) -> None:
    _seed(db, SnapshotSchedule, _schedule(1, OTHER_ORG, OLD), _schedule(2, ORG, NEW))
    before = _state(db)

    counts = await _move(db)

    assert counts["schedules"] == 0
    assert _state(db) == before


async def test_only_exact_rules_of_owned_local_policies_move(db: AsyncSessionFacade) -> None:
    _seed(db, TenantPolicyRule,
          _rule(LOCAL, OLD, ["view"]), _rule(ORCAHUB, OLD, ["view"]), _rule(FOREIGN, OLD, ["view"]),
          _rule(LOCAL, OLD, ["operate"], instance="*"))

    counts = await _move(db)

    assert counts["tenant_rules"] == 1
    assert _state(db)["rules"] == {
        (LOCAL, "cluster-b", "tenant-b", "web", ("view",)),
        (ORCAHUB, "cluster-a", "tenant-a", "web", ("view",)),
        (FOREIGN, "cluster-a", "tenant-a", "web", ("view",)),
        (LOCAL, "cluster-a", "tenant-a", "*", ("operate",)),
    }


async def test_a_rule_already_at_the_target_absorbs_the_moving_permissions(db: AsyncSessionFacade) -> None:
    _seed(db, TenantPolicyRule, _rule(LOCAL, OLD, ["view", "operate"]), _rule(LOCAL, NEW, ["view", "console"]))

    counts = await _move(db)

    assert counts["tenant_rules"] == 1
    assert _state(db)["rules"] == {(LOCAL, "cluster-b", "tenant-b", "web", ("console", "operate", "view"))}


async def test_the_same_identity_is_a_no_op(db: AsyncSessionFacade) -> None:
    _seed(db, TagAssignment, _assignment(1, OLD))
    same = InstanceIdentity(OLD.cluster_id, OLD.project, OLD.name, cluster_fingerprint="fp-z", uuid="uuid-z")

    counts = await _move(db, OLD, same)

    assert counts == {"tags": 0, "schedules": 0, "tenant_rules": 0}
    assert db.statements == []


def _seed_one_of_each(db: AsyncSessionFacade) -> None:
    _seed(db, TagAssignment, _assignment(1, OLD), _assignment(2, OLD))
    _seed(db, SnapshotSchedule, _schedule(1, ORG, OLD))
    _seed(db, TenantPolicyRule, _rule(LOCAL, OLD, ["view"]), _rule(LOCAL, OLD, ["operate"], instance="*"))


async def test_a_rollback_move_restores_every_record(db: AsyncSessionFacade) -> None:
    _seed_one_of_each(db)
    before = _state(db)

    forward = await _move(db)
    back = await _move(db, NEW, OLD)

    assert forward == back == {"tags": 2, "schedules": 1, "tenant_rules": 1}
    assert _state(db) == before


async def test_the_move_runs_inside_the_callers_transaction(db: AsyncSessionFacade) -> None:
    _seed_one_of_each(db)
    before = _state(db)

    await _move(db)
    assert _state(db) != before
    db.sync.rollback()

    assert _state(db) == before


async def test_an_orphan_schedule_at_the_new_identity_is_replaced_whatever_its_organization(
    db: AsyncSessionFacade,
) -> None:
    _seed(db, SnapshotSchedule, _schedule(1, ORG, OLD), _schedule(3, OTHER_ORG, NEW, name="web"))

    counts = await _move(db)

    ids = {row[0] for row in _state(db)["schedules"]}
    assert counts["schedules"] == 1
    assert ids == {1}
