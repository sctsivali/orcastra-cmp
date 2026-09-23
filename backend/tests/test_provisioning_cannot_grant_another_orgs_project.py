"""Provisioning may not grant a project another organization's policies already name.

A '*' grant covers every project on the cluster, including another organization's. The rule
provisioning writes widens the key creator's Partner bound and hands the listed emails that
project. A project nobody names yet, which is every freshly purchased one, still provisions.
"""

from typing import Any, List

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core import org_scope
from app.routers.integrations import ProvisionTenantAccessRequest, provision_tenant_access


class _ReachedTheBody(Exception):
    """The claim check let the request through to the org lookup."""


class _Result:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def all(self) -> List[Any]:
        return list(self._rows)

    def scalars(self) -> "_Result":
        return self


class FakeSession:
    def __init__(self, holders: List[tuple], own_ids: set) -> None:
        self.holders = holders
        self.own_ids = own_ids
        self.added: List[Any] = []

    async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> _Result:
        text = str(stmt)
        if "tenant_policy_rules" in text:
            return _Result(self.holders)
        if "organization_members" in text:
            return _Result([(org_id, None) for org_id in sorted(self.own_ids)])
        raise _ReachedTheBody(text)

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _key() -> dict:
    return {
        "id": "oak_test",
        "created_by": "partner-a@example.test",
        "cluster_permissions": [
            {"cluster_id": "cluster-a", "projects": ["*"], "permissions": ["write"]}
        ],
    }


def _body(project: str = "proj-b") -> ProvisionTenantAccessRequest:
    return ProvisionTenantAccessRequest(
        organization_external_id="22222222-2222-2222-2222-222222222222",
        cluster_id="cluster-a",
        project_name=project,
        user_emails=["tenant@example.test"],
        organization_name="Buyer",
        organization_slug="buyer",
    )


async def test_a_project_another_org_holds_is_refused():
    db = FakeSession([(2, None)], set())
    with pytest.raises(HTTPException) as caught:
        await provision_tenant_access(body=_body(), api_key=_key(), db=db)
    assert caught.value.status_code == 403
    assert "proj-b" in str(caught.value.detail)
    assert "2" not in str(caught.value.detail).replace("proj-b", ""), "no foreign org id"
    assert db.added == []


async def test_an_org_owned_by_another_key_is_refused():
    db = FakeSession([(9, "oak_other")], set())
    with pytest.raises(HTTPException) as caught:
        await provision_tenant_access(body=_body(), api_key=_key(), db=db)
    assert caught.value.status_code == 403


async def test_the_keys_own_org_passes():
    """A downgrade or restore re-provisions a project the key's own org already holds."""
    with pytest.raises(_ReachedTheBody):
        await provision_tenant_access(
            body=_body(), api_key=_key(), db=FakeSession([(5, "oak_test")], set())
        )


async def test_the_creators_own_org_passes():
    with pytest.raises(_ReachedTheBody):
        await provision_tenant_access(
            body=_body(), api_key=_key(), db=FakeSession([(1, None)], {1})
        )


async def test_a_new_project_passes():
    """The purchase path: a freshly created project has no holder yet."""
    with pytest.raises(_ReachedTheBody):
        await provision_tenant_access(body=_body("cust-buyer-0001"), api_key=_key(), db=FakeSession([], set()))


async def test_one_foreign_holder_among_own_ones_is_refused():
    db = FakeSession([(1, None), (5, "oak_test"), (2, None)], {1})
    with pytest.raises(HTTPException) as caught:
        await provision_tenant_access(body=_body(), api_key=_key(), db=db)
    assert caught.value.status_code == 403


async def test_the_check_does_not_lean_on_the_dashboard_scope():
    token = org_scope._visible_projects_var.set(None)
    try:
        with pytest.raises(HTTPException) as caught:
            await provision_tenant_access(
                body=_body(), api_key=_key(), db=FakeSession([(2, None)], set())
            )
    finally:
        org_scope._visible_projects_var.reset(token)
    assert caught.value.status_code == 403


@pytest.mark.parametrize("project", ["*", "", "a b", "../x", "x" * 64, "-lead", "p?q"])
def test_a_wildcard_or_malformed_project_is_refused_at_the_boundary(project):
    with pytest.raises(ValidationError):
        _body(project)


@pytest.mark.parametrize("project", ["cust-buyer-0f3c9a", "default", "proj_a.1", "x" * 63])
def test_real_project_names_construct(project):
    assert _body(project).project_name == project


async def test_a_tenant_row_of_the_creator_does_not_make_another_orgs_project_its_own():
    """The creator's side is the orgs it runs, the orgs its Partner bound widens from. A tenant row in
    another partner's organization is how that partner lets the creator in, not a claim on its projects."""
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles

    from app.models.db_models import (
        Organization,
        OrganizationMember,
        TenantAccessPolicy,
        TenantPolicyRule,
    )
    from app.routers.integrations import _refuse_a_project_another_party_holds
    from tests._sqlite_session import sqlite_session

    @compiles(JSONB, "sqlite")
    def _json(_element, _compiler, **_kw):
        return "JSON"

    creator = "partner-a@example.test"
    db = sqlite_session(Organization, OrganizationMember, TenantAccessPolicy, TenantPolicyRule)
    db.add_all([
        Organization(id=6, name="home", slug="home", created_by="system", is_active=True, api_key_id="oak_home"),
        Organization(id=7, name="other", slug="other", created_by="system", is_active=True, api_key_id="oak_other"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=6, user_email=creator, role="partner"),
        OrganizationMember(organization_id=7, user_email=creator, role="tenant"),
        TenantAccessPolicy(id=1, organization_id=6, name="own", created_by="system"),
        TenantAccessPolicy(id=2, organization_id=7, name="theirs", created_by="system"),
    ])
    await db.commit()
    db.add_all([
        TenantPolicyRule(policy_id=1, cluster_id="cluster-a", project_name="own-web", instance_name="*", permissions=["view"]),
        TenantPolicyRule(policy_id=2, cluster_id="cluster-a", project_name="other-web", instance_name="*", permissions=["view"]),
    ])
    await db.commit()
    key = dict(_key(), id="oak_minted")
    with pytest.raises(HTTPException) as caught:
        await _refuse_a_project_another_party_holds(db, key, "cluster-a", "other-web")
    assert caught.value.status_code == 403
    await _refuse_a_project_another_party_holds(db, key, "cluster-a", "own-web")

