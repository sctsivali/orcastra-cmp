"""Before a binding is made, the preview says what it would change, and leaves nothing behind.

A second organization on a cluster changes it for everyone: partners there see only `default` and
their own projects, the write policy becomes the shared one, and when the new organization belongs
to another party the holders lose the cluster's own tools (certificates, storage pools, networks).
The preview reads that from the real party rules by trying the row inside a savepoint that is
always rolled back. A caller refused the bind is refused the preview the same way, and learns
nothing else about the cluster.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import func, select

from app.core.auth import AuthenticatedUser
from app.models.db_models import OrganizationCluster, TenantAccessPolicy, TenantPolicyRule
from app.routers import organizations
from app.services import cluster_bind_preview
from tests.test_whole_cluster_state_needs_the_whole_cluster import ADMIN, OWN, SELLER, SOLO
from tests.test_whole_cluster_state_needs_the_whole_cluster import _world as _base_world


async def _world():
    return await _base_world(TenantAccessPolicy, TenantPolicyRule)


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


@pytest.fixture
def audited(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    asked: List[str] = []

    async def audit(cluster_id: str) -> Any:
        asked.append(cluster_id)
        return SimpleNamespace(as_dict=lambda: {"cluster_id": cluster_id, "safe": True, "complete": True,
                                                "resources_checked": {}, "findings": []})

    monkeypatch.setattr(cluster_bind_preview, "audit_shared_cluster", audit)
    return asked


async def _rows(db: Any) -> int:
    return (await db.execute(select(func.count()).select_from(OrganizationCluster))).scalar_one()


async def test_an_admin_binding_another_partys_org_is_told_the_holders_lose_the_cluster_tools(audited) -> None:
    db = await _world()
    before = await _rows(db)
    preview = await organizations.preview_cluster_bind(1, SOLO, _user(ADMIN, "role_admin"), db)
    assert preview.allowed and preview.binding_kind == "granted"
    assert preview.several_organizations_before is False and preview.several_organizations_after is True
    assert preview.holders_lose_whole_cluster_tools is True
    assert [o.name for o in preview.organizations_on_cluster] == ["solo"]
    assert preview.share_audit is not None and preview.share_audit["safe"] is True and audited == [SOLO]
    assert await _rows(db) == before, "the savepoint leaves no row"


async def test_a_bind_inside_the_holders_own_party_keeps_their_tools(audited) -> None:
    db = await _world()
    preview = await organizations.preview_cluster_bind(3, SOLO, _user(ADMIN, "role_admin"), db)
    assert preview.allowed and preview.several_organizations_after is True
    assert preview.holders_lose_whole_cluster_tools is False


async def test_an_organization_already_on_the_cluster_says_so(audited) -> None:
    db = await _world()
    preview = await organizations.preview_cluster_bind(1, OWN, _user(ADMIN, "role_admin"), db)
    assert preview.already_bound is True
    assert preview.share_audit is None and audited == []


async def test_a_partner_refused_the_bind_is_refused_the_preview_and_told_nothing_else(audited, monkeypatch) -> None:
    async def runs(_db: Any, _org: int, _email: str, _role: str) -> bool:
        return True

    monkeypatch.setattr(organizations, "user_runs_org", runs)
    db = await _world()
    preview = await organizations.preview_cluster_bind(1, SOLO, _user(SELLER), db)
    assert preview.allowed is False
    assert preview.refusal
    assert preview.organizations_on_cluster is None and preview.several_organizations_after is None
    assert audited == []


async def test_a_partner_who_does_not_run_the_organization_is_refused(audited, monkeypatch) -> None:
    async def runs(_db: Any, _org: int, _email: str, _role: str) -> bool:
        return False

    monkeypatch.setattr(organizations, "user_runs_org", runs)
    db = await _world()
    preview = await organizations.preview_cluster_bind(3, OWN, _user(SELLER), db)
    assert preview.allowed is False and preview.refusal == "No access to this organization"


async def test_the_candidates_for_a_partner_are_the_clusters_its_party_holds_whole(audited, monkeypatch) -> None:
    async def runs(_db: Any, _org: int, _email: str, _role: str) -> bool:
        return True

    async def not_bought(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(organizations, "user_runs_org", runs)
    monkeypatch.setattr(organizations, "reaches_only_by_purchase", not_bought)
    db = await _world()
    # org 2 is the seller's buyer org; 'own' is already bound to it, 'mixed' is held beside another
    # party and 'solo' only reached through a member row.
    candidates = await organizations.list_cluster_bind_candidates(2, _user(SELLER), db)
    assert candidates == []
    candidates = await organizations.list_cluster_bind_candidates(3, _user(SELLER), db)
    assert candidates == [OWN]


# ── a binding change reaches keys and lists at once ────────────────────────────────────────────

@pytest.fixture
def side_effects(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
    seen: Dict[str, List[str]] = {"bounds": [], "access": []}
    monkeypatch.setattr(organizations, "forget_cluster_bounds", lambda cluster_id: seen["bounds"].append(cluster_id))
    monkeypatch.setattr(organizations, "_access_changed", lambda: seen["access"].append("x"))
    return seen


async def test_a_bind_and_an_unbind_forget_the_key_bounds_and_the_shared_lists(side_effects, monkeypatch) -> None:
    from contextlib import asynccontextmanager
    from app.services.organization_service import OrganizationService

    @asynccontextmanager
    async def lock(_db: Any, _cluster: str):
        yield

    async def runs(*_a: Any) -> bool:
        return True

    async def exists(self: Any, org_id: int) -> Any:
        return {"id": org_id}

    async def bind(self: Any, org_id: int, cluster_id: str, registered_by: Any, granted_by: Any = None) -> Any:
        return SimpleNamespace(id=1, organization_id=org_id, cluster_id=cluster_id, registered_by=registered_by,
                               granted_by=granted_by)

    async def unbind(self: Any, org_id: int, cluster_id: str) -> bool:
        return True

    monkeypatch.setattr(organizations, "_guard_cluster_write", lock)
    monkeypatch.setattr(organizations, "user_runs_org", runs)
    monkeypatch.setattr(OrganizationService, "get_organization", exists)
    monkeypatch.setattr(OrganizationService, "bind_cluster", bind)
    monkeypatch.setattr(OrganizationService, "unbind_cluster", unbind)
    db = await _world()
    await organizations.bind_cluster(1, organizations.ClusterBindRequest(cluster_id=SOLO), _user(ADMIN, "role_admin"), db)
    await organizations.unbind_cluster(1, SOLO, _user(ADMIN, "role_admin"), db)
    assert side_effects["bounds"] == [SOLO, SOLO]
    assert side_effects["access"] == ["x", "x"]
