"""Database-backed plan blockers: OrcaHub-billed projects and endpoints held by another job.

The OrcaHub checks run against SQLite tables copied with JSON for JSONB, so the join and
every WHERE term are real. The endpoint check reads one MigrationJob row, which is scripted.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy import JSON, MetaData, insert
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization, TenantAccessPolicy, TenantPolicyRule
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.core.rbac import Role
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy
from app.services.migrations import planning_checks
from app.services.migrations.planning_checks import (
    active_endpoint_blockers,
    orcahub_blockers,
    shared_policy_blockers,
    stale_target_access_blockers,
)
from app.services.migrations.project_policy import clone_config
from app.services.migrations.security import canonical_resource_key
from tests._sqlite_session import AsyncSessionFacade, sqlite_session

HUB_POLICY, LOCAL_POLICY = 1, 2
LOCKED = SimpleNamespace(migration_orcahub_identity_change_enabled=False)


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    for model in (Organization, TenantAccessPolicy, TenantPolicyRule):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(facade.sync.get_bind())
    rows = {
        Organization: [{"id": 1, "name": "Org", "slug": "org", "created_by": "ops@example.test"}],
        TenantAccessPolicy: [
            {"id": policy, "organization_id": 1, "name": source, "source": source, "created_by": "ops@example.test"}
            for policy, source in ((HUB_POLICY, "orcahub"), (LOCAL_POLICY, "local"))
        ],
        TenantPolicyRule: [
            {"policy_id": policy, "cluster_id": cluster, "project_name": project, "instance_name": "*",
             "permissions": ["view"]}
            for policy, cluster, project in (
                (HUB_POLICY, "hub", "billed"), (HUB_POLICY, "wild", "*"), (LOCAL_POLICY, "plain", "tenant-a"),
            )
        ],
    }
    for model, values in rows.items():
        facade.sync.execute(insert(model), values)
    facade.sync.commit()
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


def _request(
    *,
    intent: str = "move",
    source: tuple[str, str] = ("plain", "tenant-a"),
    target: tuple[str, str] = ("plain", "tenant-b"),
    target_name: Optional[str] = None,
) -> MigrationPlanCreateRequest:
    target_ref: dict[str, Any] = {"cluster_id": target[0], "project": target[1]}
    if target_name is not None:
        target_ref["resource_name"] = target_name
    return MigrationPlanCreateRequest.model_validate({
        "scope": "instance",
        "intent": intent,
        "source": {"cluster_id": source[0], "project": source[1], "resource_name": "web"},
        "target": target_ref,
    })


@pytest.mark.parametrize(
    ("intent", "source", "target", "sides"),
    [
        ("move", ("hub", "billed"), ("plain", "tenant-b"), ["source"]),
        ("move", ("plain", "tenant-a"), ("hub", "billed"), ["target"]),
        ("move", ("hub", "billed"), ("wild", "anything"), ["source", "target"]),
        ("copy", ("hub", "billed"), ("plain", "tenant-b"), []),
        ("copy", ("plain", "tenant-a"), ("hub", "billed"), ["target"]),
        ("move", ("plain", "billed"), ("hub", "other"), []),
        ("move", ("plain", "tenant-a"), ("plain", "tenant-b"), []),
    ],
)
async def test_orcahub_billed_projects_block_identity_changes(
    db: AsyncSessionFacade, intent: str, source: tuple[str, str], target: tuple[str, str], sides: list[str]
) -> None:
    request = _request(intent=intent, source=source, target=target)

    blockers = await orcahub_blockers(db, request, strategy="cross_cluster_pull", settings=LOCKED)

    assert [blocker["message"].split()[1] for blocker in blockers] == sides
    for blocker in blockers:
        assert blocker["code"] == "orcahub_managed_project_identity_change"
        assert blocker["severity"] == "blocker"


async def test_the_blocker_names_the_billed_project(db: AsyncSessionFacade) -> None:
    request = _request(target=("hub", "billed"))

    [blocker] = await orcahub_blockers(db, request, strategy="cross_cluster_pull", settings=object())

    assert "'billed'" in blocker["message"]
    assert blocker["remediation"]


@pytest.mark.parametrize(
    ("strategy", "settings"),
    [
        ("member_relocation", LOCKED),
        ("cross_cluster_pull", SimpleNamespace(migration_orcahub_identity_change_enabled=True)),
    ],
)
async def test_orcahub_checks_are_skipped_when_the_identity_does_not_change_or_is_allowed(
    db: AsyncSessionFacade, strategy: str, settings: Any
) -> None:
    request = _request(source=("hub", "billed"), target=("hub", "billed"))

    assert await orcahub_blockers(db, request, strategy=strategy, settings=settings) == []
    assert db.statements == []


class _OneRow:
    """Answers the endpoint query with one scripted (job_id, phase, scope) row, or none."""

    def __init__(self, row: Optional[tuple[str, str, str]]) -> None:
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return SimpleNamespace(first=lambda: self.row)


async def test_a_free_endpoint_has_no_blocker() -> None:
    assert await active_endpoint_blockers(_OneRow(None), _request()) == []


@pytest.mark.parametrize("phase", ["source_retained", "cleanup_due", "cleanup_blocked"])
async def test_a_retained_endpoint_asks_for_finalize_or_rollback(phase: str) -> None:
    [blocker] = await active_endpoint_blockers(_OneRow(("job-1", phase, "instance")), _request())

    assert blocker["code"] == "endpoint_in_active_migration"
    assert "retained source or target" in blocker["message"]
    assert blocker["remediation"].startswith("Finalize or roll back")


@pytest.mark.parametrize("phase", ["queued", "transferring", "activating"])
async def test_a_running_endpoint_asks_to_wait(phase: str) -> None:
    [blocker] = await active_endpoint_blockers(_OneRow(("job-1", phase, "instance")), _request())

    assert "still running" in blocker["message"]
    assert blocker["remediation"] == "Wait for the other migration to finish."


@pytest.mark.parametrize(("target_name", "expected_name"), [(None, "web"), ("web-copy", "web-copy")])
async def test_the_endpoint_query_matches_both_keys_on_either_side(
    target_name: Optional[str], expected_name: str
) -> None:
    db = _OneRow(None)
    request = _request(target=("hub", "tenant-b"), target_name=target_name)

    await active_endpoint_blockers(db, request)

    keys = {
        canonical_resource_key("instance", "plain", "tenant-a", "web"),
        canonical_resource_key("instance", "hub", "tenant-b", expected_name),
        # A project relocation keeps its project keys active through retention.
        canonical_resource_key("project", "plain", "tenant-a", None),
        canonical_resource_key("project", "hub", "tenant-b", None),
    }
    params = db.statements[0].compile().params
    assert [set(value) for value in params.values() if isinstance(value, list)] == [keys, keys]


@pytest.mark.parametrize(("phase", "words"), [("source_retained", "still in retention"), ("final_sync", "still running")])
async def test_a_project_relocation_blocks_every_plan_that_touches_its_projects(phase: str, words: str) -> None:
    """The new project holds no lock during retention, so an instance moved out of it would
    become another job's retained source that the relocation's rollback deletes."""
    [blocker] = await active_endpoint_blockers(_OneRow(("job-1", phase, "project")), _request())

    assert "project relocation" in blocker["message"] and words in blocker["message"]


def _exact_rule(db: AsyncSessionFacade, policy: int, cluster: str, project: str, name: str) -> None:
    db.sync.execute(insert(TenantPolicyRule), [{
        "policy_id": policy, "cluster_id": cluster, "project_name": project, "instance_name": name,
        "permissions": ["view", "operate"],
    }])
    db.sync.commit()


async def test_a_move_onto_a_name_with_leftover_access_rules_is_blocked(db: AsyncSessionFacade) -> None:
    _exact_rule(db, LOCAL_POLICY, "plain", "tenant-b", "web")

    [blocker] = await stale_target_access_blockers(db, _request(), organization_id=1)

    assert blocker["code"] == "target_access_rules_exist"
    assert "'web'" in blocker["message"]


@pytest.mark.parametrize(
    ("policy", "intent", "organization_id", "target_name"),
    [
        (HUB_POLICY, "move", 1, None),
        (LOCAL_POLICY, "copy", 1, None),
        (LOCAL_POLICY, "move", 2, None),
        (LOCAL_POLICY, "move", 1, "web-new"),
    ],
)
async def test_leftover_rules_only_block_a_move_of_the_same_organization_onto_that_name(
    db: AsyncSessionFacade,
    policy: int,
    intent: str,
    organization_id: int,
    target_name: Optional[str],
) -> None:
    _exact_rule(db, policy, "plain", "tenant-b", "web")
    request = _request(intent=intent, target_name=target_name)

    assert await stale_target_access_blockers(db, request, organization_id=organization_id) == []


def _shared_partner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def policy(_user: Any, cluster_id: str, _db: Any) -> ClusterWritePolicy:
        return ClusterWritePolicy(cluster_id=cluster_id, role=Role.PARTNER, ownership=ClusterOwnership.SHARED)

    monkeypatch.setattr(planning_checks, "resolve_cluster_write_policy", policy)


ISOLATED = {
    "restricted": "true",
    "features.networks": "true",
    "features.images": "true",
    "features.profiles": "true",
    "features.storage.volumes": "true",
}


@pytest.mark.parametrize(
    ("source_config", "blocked"),
    [
        ({}, True),
        ({**ISOLATED, "restricted": "false"}, True),
        ({**ISOLATED, "features.networks": "false"}, True),
        ({**ISOLATED, "limits.cpu": "8"}, True),
        (ISOLATED, False),
        ({**ISOLATED, "user.owner": "team-a"}, False),
    ],
)
async def test_a_partner_on_a_shared_cluster_only_gets_an_isolated_new_project(
    monkeypatch: pytest.MonkeyPatch, source_config: dict[str, str], blocked: bool
) -> None:
    _shared_partner(monkeypatch)
    clone = clone_config({"config": source_config}, job_id=None)

    blockers = await shared_policy_blockers(
        None,
        user=SimpleNamespace(),
        request=_request(),
        contracts={},
        target_project_config=clone,
        creates_project=True,
    )

    codes = [blocker["code"] for blocker in blockers]
    assert codes == (["shared_cluster_target_project_not_isolated"] if blocked else [])
