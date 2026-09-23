"""Binding a cluster to an organization must need ownership of the cluster, not only of the org.

`POST /organizations/{id}/clusters` checked `user_has_access` on the organization alone. A
partner could create an organization and bind any cluster id to it, and org scope is derived
from exactly that row, so they then reached the cluster. The row also stamped `registered_by`
with the caller, which `_query_owned_clusters` treats as ownership, so an admin binding a
foreign cluster became its owner and skipped break-glass.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import organization_clusters as organizations


class _RecordingService:
    calls: list[dict[str, Any]] = []

    def __init__(self, _db: Any) -> None:
        pass

    async def user_has_access(self, _org_id: int, _email: str, _role: str) -> bool:
        return True

    async def bind_cluster(
        self, org_id: int, cluster_id: str, registered_by: Optional[str]
    ) -> SimpleNamespace:
        _RecordingService.calls.append(
            {"org_id": org_id, "cluster_id": cluster_id, "registered_by": registered_by}
        )
        return SimpleNamespace(
            id=1, organization_id=org_id, cluster_id=cluster_id, registered_by=registered_by
        )


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingService]:
    _RecordingService.calls = []
    monkeypatch.setattr(organizations, "OrganizationService", _RecordingService)
    return _RecordingService


def _owning(monkeypatch: pytest.MonkeyPatch, owned: set[str]) -> None:
    async def fake_owned(_user: AuthenticatedUser, _db: Any) -> set[str]:
        return set(owned)

    monkeypatch.setattr(organizations, "get_owned_clusters_ignoring_admin", fake_owned)


def _user(role: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{role}-bind-probe", email=f"{role}@example.test", groups=[f"role_{role}"]
    )


def _bind(role: str, cluster_id: str) -> Any:
    return asyncio.run(
        organizations.bind_cluster(
            org_id=7,
            body=organizations.ClusterBindRequest(cluster_id=cluster_id),
            user=_user(role),
            db=None,
        )
    )


def test_a_partner_cannot_bind_a_cluster_they_do_not_own(
    service: type[_RecordingService], monkeypatch: pytest.MonkeyPatch
) -> None:
    _owning(monkeypatch, {"cluster-a"})
    with pytest.raises(HTTPException) as raised:
        _bind("partner", "cluster-foreign")
    assert raised.value.status_code == 403
    assert raised.value.detail == "Not permitted to bind cluster cluster-foreign"
    assert service.calls == []


def test_a_partner_binds_their_own_cluster_and_is_stamped(
    service: type[_RecordingService], monkeypatch: pytest.MonkeyPatch
) -> None:
    _owning(monkeypatch, {"cluster-a"})
    response = _bind("partner", "cluster-a")
    assert service.calls == [
        {"org_id": 7, "cluster_id": "cluster-a", "registered_by": "partner@example.test"}
    ]
    assert response.registered_by == "partner@example.test"


def test_an_admin_binding_a_foreign_cluster_is_not_stamped_as_its_owner(
    service: type[_RecordingService], monkeypatch: pytest.MonkeyPatch
) -> None:
    _owning(monkeypatch, {"cluster-a"})
    response = _bind("admin", "cluster-foreign")
    assert service.calls == [
        {"org_id": 7, "cluster_id": "cluster-foreign", "registered_by": None}
    ]
    assert response.registered_by is None


def test_an_admin_binding_a_cluster_they_own_keeps_the_stamp(
    service: type[_RecordingService], monkeypatch: pytest.MonkeyPatch
) -> None:
    _owning(monkeypatch, {"cluster-a"})
    _bind("admin", "cluster-a")
    assert service.calls[0]["registered_by"] == "admin@example.test"



def test_the_frontend_types_the_registrant_as_nullable() -> None:
    """An admin's binding of a foreign cluster now returns registered_by null to the UI."""
    from pathlib import Path

    types = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "api.ts"
    source = types.read_text(encoding="utf-8")
    block = source[source.index("export interface OrganizationCluster {"):]
    block = block[: block.index("}")]
    assert "registered_by: string | null;" in block
