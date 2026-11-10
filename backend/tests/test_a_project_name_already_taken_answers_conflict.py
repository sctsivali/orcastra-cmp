"""Creating a project under a name the cluster already has answers 409, not 400.

A conflict is the caller's to resolve by choosing another name or reusing the project, and the
other create routes (images, profiles, networks through lxd_http_error) also answer 409. Any other
refusal LXD gives the create keeps its 400.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.models.schemas import ProjectCreateRequest
from app.routers import projects as projects_router
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy
from app.services.lxd.exceptions import LXDOperationError

HOST = "c1"


def _create(monkeypatch, refusal: str) -> HTTPException:
    @asynccontextmanager
    async def no_op_lock(*_a, **_k):
        yield

    async def whole_policy(*_a, **_k):
        return ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.EXCLUSIVE)

    async def resources(*_a, **_k):
        return {}

    async def no_fence(*_a, **_k):
        return None

    async def grant_owner(*_a, **_k):
        return None

    class _Service:
        @staticmethod
        async def create_project(**_kwargs):
            raise LXDOperationError(refusal)

    monkeypatch.setattr(projects_router, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(projects_router, "_bounded_node_resources", resources)
    monkeypatch.setattr(projects_router, "_refuse_cap_above_hardware", lambda *_a, **_k: None)
    monkeypatch.setattr(projects_router, "serialize_cluster_configuration", no_op_lock)
    monkeypatch.setattr(projects_router, "_cluster_write_policy", whole_policy)
    monkeypatch.setattr(projects_router, "assert_project_not_migration_fenced", no_fence)
    monkeypatch.setattr(projects_router, "org_to_hold_project_grant", grant_owner)
    monkeypatch.setattr(projects_router, "activity_logger", SimpleNamespace(log_activity=lambda **_k: None))
    request = ProjectCreateRequest(name="qa-taken", host=HOST, description="", config={}, max_cpu=2,
                                   max_ram="2GiB", max_storage="20GiB")
    user = AuthenticatedUser(user_id="p", email="p@example.test", roles=["role_partner"], groups=["role_partner"])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(projects_router.create_project(request, lxd_service=_Service(), _=None, user=user,
                                                   db=SimpleNamespace()))
    return caught.value


def test_a_taken_name_is_a_conflict(monkeypatch) -> None:
    refused = _create(monkeypatch, 'Could not create project: Project "qa-taken" already exists')
    assert refused.status_code == 409


def test_any_other_refusal_stays_a_bad_request(monkeypatch) -> None:
    refused = _create(monkeypatch, 'Could not create project: Invalid value for "limits.cpu"')
    assert refused.status_code == 400
