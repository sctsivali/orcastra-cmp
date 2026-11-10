"""Binding a cluster into an organization, or registering one that exists, needs the whole cluster.

POST /organizations/{id}/clusters and cluster registration must not judge ownership from any
membership, a tenant row included. A partner that only buys from another partner reaches that
partner's cluster through its member row; binding it into an organization of its own would stamp it
as the registrant and make the cluster its own. Registering a cluster again rewrites its endpoint and
certificate for every holder, so a mere member of a holding organization may not do it either. The
party is read from real rows on SQLite; the share audit and the binding write are recorders.
"""

from contextlib import asynccontextmanager
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.whole_cluster_access import SHARED_CLUSTER_DETAIL
from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from app.routers import cluster_registration, organizations
from app.services.organization_service import OrganizationService
from tests.test_whole_cluster_state_needs_the_whole_cluster import ADMIN, MIXED, OWN, SELLER, SOLO, _world

TEAMMATE = "teammate@example.test"


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


@pytest.fixture
def binding(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """The organization check, the cluster write lock, the purchase check and the binding, recorded."""
    written: List[Dict[str, Any]] = []

    async def _runs(_db: Any, org_id: int, email: str, role: str) -> bool:
        return True

    @asynccontextmanager
    async def _lock(_db: Any, _cluster: str):
        yield

    async def _bind(self: Any, org_id: int, cluster_id: str, registered_by: Any) -> Any:
        written.append({"org": org_id, "cluster": cluster_id, "registered_by": registered_by})
        return {"id": 1, "organization_id": org_id, "cluster_id": cluster_id, "registered_by": registered_by}

    async def _not_bought(*_a: Any, **_k: Any) -> bool:
        return False

    async def _exists(self: Any, org_id: int) -> Any:
        return {"id": org_id}

    monkeypatch.setattr(organizations, "user_runs_org", _runs)
    monkeypatch.setattr(OrganizationService, "get_organization", _exists)
    monkeypatch.setattr(OrganizationService, "bind_cluster", _bind)
    monkeypatch.setattr(organizations, "_guard_cluster_write", _lock)
    monkeypatch.setattr(organizations, "reaches_only_by_purchase", _not_bought)
    monkeypatch.setattr(organizations.ClusterBindingResponse, "model_validate", classmethod(lambda cls, row: row))
    return written


async def _bind(cluster: str, user: AuthenticatedUser) -> Any:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    return await organizations.bind_cluster(9, organizations.ClusterBindRequest(cluster_id=cluster), user, db)


async def test_the_seller_binds_its_own_cluster_after_a_sale_and_is_stamped(binding) -> None:
    await _bind(OWN, _user(SELLER))
    assert binding == [{"org": 9, "cluster": OWN, "registered_by": SELLER}]


@pytest.mark.parametrize("cluster", [SOLO, MIXED], ids=["reached-by-a-member-row", "held-beside-another-party"])
async def test_a_cluster_another_party_holds_is_never_bound(binding, cluster: str) -> None:
    with pytest.raises(HTTPException) as caught:
        await _bind(cluster, _user(SELLER))
    assert caught.value.status_code == 403 and caught.value.detail == SHARED_CLUSTER_DETAIL
    assert binding == []


async def test_an_admin_binding_a_cluster_it_does_not_own_stamps_no_registrant(binding) -> None:
    await _bind(SOLO, _user(ADMIN, "role_admin"))
    assert binding == [{"org": 9, "cluster": SOLO, "registered_by": None}]


async def _register(cluster: str, user: AuthenticatedUser) -> Any:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    return await cluster_registration._guard_registration_binding(db, cluster, user)


async def test_registering_a_cluster_another_party_holds_is_refused() -> None:
    with pytest.raises(HTTPException) as caught:
        await _register(SOLO, _user(SELLER))
    assert caught.value.status_code == 403 and caught.value.detail == SHARED_CLUSTER_DETAIL


async def test_the_seller_registers_its_own_cluster_again() -> None:
    assert 1 in await _register(OWN, _user(SELLER))


async def test_an_admin_registers_any_cluster_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The party is not asked of an admin; the share audit that follows still runs."""
    audited: List[str] = []

    async def _audit(_db: Any, cluster_id: str, **_k: Any) -> None:
        audited.append(cluster_id)

    monkeypatch.setattr(cluster_registration, "assert_cluster_safe_to_share", _audit)
    await _register(SOLO, _user(ADMIN, "role_admin"))
    assert audited == [SOLO]
