"""A whole project only relocates between clusters that each belong to one organization.

Ownership of a project that does not exist yet can only be proven through the cluster binding,
so a shared cluster refuses relocation while a member relocation inside it stays available.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations import planning
from app.services.migrations.contracts import MigrationConflict


def _request(target_cluster: str, target_project: str, member: str | None = None) -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "project",
            "source": {"cluster_id": "cluster-a", "project": "web"},
            "target": {"cluster_id": target_cluster, "project": target_project, "member": member},
        }
    )


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def owner(_db: Any, *, cluster_id: str, project: str) -> Any:
        return SimpleNamespace(organization_id=7, resolution="exact")

    monkeypatch.setattr(planning, "resolve_instance_job_organization", owner)
    return planning.MigrationPlanningMixin(db=object(), lxd_service=object(), settings=SimpleNamespace())


@pytest.mark.parametrize(
    ("shared", "target", "refused"),
    [
        ({"cluster-b"}, ("cluster-b", "web"), True),
        ({"cluster-a"}, ("cluster-b", "web"), True),
        ({"cluster-a"}, ("cluster-a", "web-renamed"), True),
        (set(), ("cluster-b", "web"), False),
    ],
)
async def test_a_relocation_needs_single_owner_clusters(
    monkeypatch: pytest.MonkeyPatch, planner: Any, shared: set[str], target: tuple[str, str], refused: bool
) -> None:
    async def single(_db: Any, *clusters: str) -> bool:
        return not (set(clusters) & shared)

    monkeypatch.setattr(planning, "single_owner_clusters", single)
    request = _request(*target)

    if refused:
        with pytest.raises(MigrationConflict, match="each belong to one organization"):
            await planner._organizations(request, user=None, requester_role="admin")
    else:
        assert await planner._organizations(request, user=None, requester_role="admin") == (7, 7)


async def test_a_member_relocation_on_a_shared_cluster_is_not_refused(monkeypatch: pytest.MonkeyPatch, planner: Any) -> None:
    async def never(*_args: Any) -> bool:
        raise AssertionError("a member relocation keeps every identity, so ownership is not re-proven")

    monkeypatch.setattr(planning, "single_owner_clusters", never)

    assert await planner._organizations(_request("cluster-a", "web", "m2"), user=None, requester_role="admin") == (7, 7)
