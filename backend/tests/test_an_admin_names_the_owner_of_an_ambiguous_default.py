"""An Admin names which organization owns an instance in an ambiguous `default`.

`default` on a cluster several organizations hold names no owner, so Migration Center cannot
resolve one for a move out of it. An Admin names the owner. It is never picked for them, it must
be an organization bound to the source cluster that also owns the target (a plan's source and
target belong to one organization), and partners still cannot move out of a view-only `default`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations import planning
from app.services.migrations.contracts import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationOwnerChoiceRequired,
)

BOUND = [(6, "acme", "Acme"), (7, "globex", "Globex"), (9, "initech", "Initech")]


def _request(project: str = "default", owner: Optional[int] = None) -> MigrationPlanCreateRequest:
    body: Dict[str, Any] = {
        "scope": "instance",
        "source": {"cluster_id": "shared", "project": project, "resource_name": "web"},
        "target": {"cluster_id": "mine", "project": "web"},
    }
    if owner is not None:
        body["source_organization_id"] = owner
    return MigrationPlanCreateRequest.model_validate(body)


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def resolve(_db: Any, *, cluster_id: str, project: str) -> Any:
        if cluster_id == "shared" and project == "default":
            return SimpleNamespace(organization_id=None, organization_slug=None, resolution="ambiguous")
        if cluster_id == "shared":
            return SimpleNamespace(organization_id=9, organization_slug="initech", resolution="exact")
        return SimpleNamespace(organization_id=7, organization_slug="globex", resolution="exact")

    async def bound(_db: Any, cluster_id: str):
        assert cluster_id == "shared"
        return list(BOUND)

    monkeypatch.setattr(planning, "resolve_instance_job_organization", resolve)
    monkeypatch.setattr(planning, "bound_organizations", bound)
    return planning.MigrationPlanningMixin(db=object(), lxd_service=object(), settings=SimpleNamespace())


async def test_an_admin_without_a_choice_is_shown_the_candidates(planner: Any) -> None:
    with pytest.raises(MigrationOwnerChoiceRequired) as asked:
        await planner._organizations(_request(), user=None, requester_role="admin")
    assert asked.value.candidates == [(7, "globex", "Globex")], "narrowed to the target's organization"


async def test_an_admin_choice_is_the_plans_organization(planner: Any) -> None:
    assert await planner._organizations(_request(owner=7), user=None, requester_role="admin") == (7, 7)


async def test_a_choice_outside_the_candidates_is_refused_with_them(planner: Any) -> None:
    for owner in (6, 9, 42):
        with pytest.raises(MigrationOwnerChoiceRequired):
            await planner._organizations(_request(owner=owner), user=None, requester_role="admin")


async def test_a_choice_against_an_exact_owner_is_refused(planner: Any) -> None:
    with pytest.raises(MigrationConflict) as refused:
        await planner._organizations(_request("web", owner=7), user=None, requester_role="admin")
    assert not isinstance(refused.value, MigrationOwnerChoiceRequired)
    assert "already belongs to one organization" in str(refused.value)


async def test_a_partner_cannot_name_an_owner(planner: Any) -> None:
    with pytest.raises(MigrationAuthorityError) as refused:
        await planner._organizations(_request(owner=7), user=None, requester_role="partner")
    assert refused.value.code == "source_owner_choice_not_allowed"


async def test_a_partner_without_a_choice_still_meets_the_ambiguity(planner: Any) -> None:
    with pytest.raises(MigrationConflict) as refused:
        await planner._organizations(_request(), user=None, requester_role="partner")
    assert not isinstance(refused.value, MigrationOwnerChoiceRequired)
    assert "ambiguous" in str(refused.value)


async def test_another_ambiguous_project_is_not_offered_a_choice(planner: Any, monkeypatch) -> None:
    async def resolve(_db: Any, *, cluster_id: str, project: str) -> Any:
        if cluster_id == "shared":
            return SimpleNamespace(organization_id=None, organization_slug=None, resolution="ambiguous")
        return SimpleNamespace(organization_id=7, organization_slug="globex", resolution="exact")

    monkeypatch.setattr(planning, "resolve_instance_job_organization", resolve)
    with pytest.raises(MigrationConflict) as refused:
        await planner._organizations(_request("web", owner=7), user=None, requester_role="admin")
    assert not isinstance(refused.value, MigrationOwnerChoiceRequired)


def test_the_choice_is_stored_with_the_request_so_patch_and_execute_check_it_again() -> None:
    stored = _request(owner=7).model_dump(mode="json")
    assert stored["source_organization_id"] == 7
    assert MigrationPlanCreateRequest.model_validate(stored).source_organization_id == 7


def test_the_route_answers_the_candidates_in_a_structured_409() -> None:
    import asyncio

    from fastapi import HTTPException

    from app.routers.migrations import _service_call

    async def asks():
        raise MigrationOwnerChoiceRequired("Name the owner.", [(7, "globex", "Globex")])

    with pytest.raises(HTTPException) as answered:
        asyncio.run(_service_call(asks()))
    assert answered.value.status_code == 409
    assert answered.value.detail == {
        "error": "source_owner_choice_required",
        "message": "Name the owner.",
        "candidates": [{"id": 7, "slug": "globex", "name": "Globex"}],
    }
