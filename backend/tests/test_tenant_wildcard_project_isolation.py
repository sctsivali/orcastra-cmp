"""A Tenant wildcard stays inside its policy organization's shared-cluster projects."""

import asyncio
from datetime import timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core import database as database_module
from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.schemas import VMInfo, VMStatus
from app.routers import console, instances, tenant_access
from app.services import lxd_service as lxd_service_module


HOST = "shared-cluster"
TENANT_EMAIL = "tenant-a@example.test"


class _AsyncSessionAdapter:
    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement):
        return self._session.execute(statement)


def _tenant() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="tenant-a",
        email=TENANT_EMAIL,
        groups=["role_tenant"],
        roles=["role_tenant"],
    )


def _database():
    """Two organizations share one cluster; only organization A assigned this Tenant."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE organizations ("
                "id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL, "
                "is_active BOOLEAN NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE organization_members ("
                "id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL, "
                "user_email TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE organization_clusters ("
                "id INTEGER PRIMARY KEY, organization_id INTEGER, cluster_id TEXT NOT NULL, "
                "registered_by TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tenant_access_policies ("
                "id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL, name TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tenant_policy_assignments ("
                "id INTEGER PRIMARY KEY, policy_id INTEGER NOT NULL, "
                "org_member_id INTEGER NOT NULL, expires_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tenant_policy_rules ("
                "id INTEGER PRIMARY KEY, policy_id INTEGER NOT NULL, "
                "cluster_id TEXT NOT NULL, project_name TEXT NOT NULL, "
                "instance_name TEXT NOT NULL, permissions TEXT NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )

        connection.execute(
            text("INSERT INTO organizations VALUES (:id, :name, :owner, :active)"),
            [
                {"id": 1, "name": "Org A", "owner": "partner-a", "active": True},
                {"id": 2, "name": "Org B", "owner": "partner-b", "active": True},
                {"id": 3, "name": "Inactive", "owner": "partner-c", "active": False},
            ],
        )
        connection.execute(
            text("INSERT INTO organization_members VALUES (:id, :org, :email)"),
            [
                {"id": 11, "org": 1, "email": TENANT_EMAIL},
                {"id": 12, "org": 2, "email": "tenant-b@example.test"},
                {"id": 13, "org": 3, "email": TENANT_EMAIL},
                {"id": 14, "org": 2, "email": TENANT_EMAIL},
            ],
        )
        connection.execute(
            text("INSERT INTO organization_clusters VALUES (:id, :org, :cluster, NULL)"),
            [
                {"id": 21, "org": 1, "cluster": HOST},
                {"id": 22, "org": 2, "cluster": HOST},
                {"id": 23, "org": 3, "cluster": HOST},
            ],
        )
        connection.execute(
            text("INSERT INTO tenant_access_policies VALUES (:id, :org, :name)"),
            [
                {"id": 31, "org": 1, "name": "A wildcard"},
                {"id": 32, "org": 1, "name": "A project ownership"},
                {"id": 33, "org": 2, "name": "B project ownership"},
                {"id": 34, "org": 2, "name": "Cross-org assignment"},
                {"id": 35, "org": 1, "name": "Expired assignment"},
                {"id": 36, "org": 3, "name": "Inactive organization"},
                {"id": 37, "org": 2, "name": "B narrow"},
            ],
        )
        connection.execute(
            text("INSERT INTO tenant_policy_assignments VALUES (:id, :policy, :member, :expires)"),
            [
                {"id": 41, "policy": 31, "member": 11, "expires": None},
                # Policy organization B does not match member organization A.
                {"id": 42, "policy": 34, "member": 11, "expires": None},
                {"id": 43, "policy": 35, "member": 11, "expires": "2020-01-01"},
                {"id": 44, "policy": 36, "member": 13, "expires": None},
                {"id": 45, "policy": 37, "member": 14, "expires": None},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO tenant_policy_rules VALUES ("
                ":id, :policy, :cluster, :project, :instance, :permissions, "
                "'2026-01-01')"
            ),
            [
                {
                    "id": 51,
                    "policy": 31,
                    "cluster": HOST,
                    "project": "*",
                    "instance": "*",
                    "permissions": '["view", "operate"]',
                },
                {
                    "id": 52,
                    "policy": 32,
                    "cluster": HOST,
                    "project": "org-a",
                    "instance": "*",
                    "permissions": '["view"]',
                },
                {
                    "id": 53,
                    "policy": 33,
                    "cluster": HOST,
                    "project": "org-b",
                    "instance": "*",
                    "permissions": '["view"]',
                },
                {
                    "id": 54,
                    "policy": 34,
                    "cluster": HOST,
                    "project": "*",
                    "instance": "cross-org",
                    "permissions": '["view"]',
                },
                {
                    "id": 55,
                    "policy": 35,
                    "cluster": HOST,
                    "project": "*",
                    "instance": "expired",
                    "permissions": '["view"]',
                },
                {
                    "id": 56,
                    "policy": 36,
                    "cluster": HOST,
                    "project": "*",
                    "instance": "inactive",
                    "permissions": '["view"]',
                },
                {
                    "id": 57,
                    "policy": 37,
                    "cluster": HOST,
                    "project": "org-b",
                    "instance": "only-b",
                    "permissions": '["view"]',
                },
            ],
        )

    session = Session(engine)
    return engine, session, _AsyncSessionAdapter(session)


async def _resolved_scope(db):
    await org_scope.org_scope_dependency(user=_tenant(), db=db)
    return (
        org_scope.get_current_allowed_clusters(),
        org_scope.get_tenant_acl(),
        org_scope.get_current_visible_projects(),
    )


def test_real_dependency_preserves_policy_org_and_expands_only_its_projects():
    engine, session, db = _database()
    try:
        allowed, acl, projects = asyncio.run(_resolved_scope(db))
    finally:
        session.close()
        engine.dispose()

    assert allowed == {HOST}
    assert acl == {
        HOST: {
            ("default", "*"): {"view", "operate"},
            ("org-a", "*"): {"view", "operate"},
            ("org-b", "only-b"): {"view"},
        }
    }
    assert projects == {HOST: {"default", "org-a", "org-b"}}


def test_websocket_scope_uses_the_same_per_org_expansion(monkeypatch):
    engine, session, db = _database()

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(database_module, "async_session_maker", _SessionContext)

    async def drive():
        await org_scope.set_org_scope_for_ws(_tenant())
        return (
            org_scope.get_current_allowed_clusters(),
            org_scope.get_tenant_acl(),
            org_scope.get_current_visible_projects(),
        )

    try:
        allowed, acl, projects = asyncio.run(drive())
    finally:
        session.close()
        engine.dispose()

    assert allowed == {HOST}
    assert acl == {
        HOST: {
            ("default", "*"): {"view", "operate"},
            ("org-a", "*"): {"view", "operate"},
            ("org-b", "only-b"): {"view"},
        }
    }
    assert projects == {HOST: {"default", "org-a", "org-b"}}


def test_provenanced_grants_exclude_cross_org_expired_and_inactive_rows():
    engine, session, db = _database()
    try:
        grants = asyncio.run(org_scope.get_tenant_policy_grants(_tenant(), db))
    finally:
        session.close()
        engine.dispose()

    assert grants is not None
    assert [
        (grant.organization_id, grant.policy_name, grant.project_name, grant.instance_name) for grant in grants
    ] == [
        (1, "A wildcard", "*", "*"),
        (2, "B narrow", "org-b", "only-b"),
    ]


def test_wildcard_and_same_name_instance_are_clamped_before_lxd(monkeypatch):
    engine, session, db = _database()
    lxd_calls = []

    class _NeverLXD:
        async def get_vm(self, *args, **kwargs):
            lxd_calls.append(("detail", args, kwargs))
            raise AssertionError("a foreign project reached LXD detail")

        async def get_instance_metrics(self, *args, **kwargs):
            lxd_calls.append(("metrics", args, kwargs))
            raise AssertionError("a foreign project reached LXD metrics")

    def _never_service():
        lxd_calls.append("console")
        raise AssertionError("a foreign project reached console LXD")

    monkeypatch.setattr(lxd_service_module, "get_lxd_service", _never_service)

    async def drive():
        await org_scope.org_scope_dependency(user=_tenant(), db=db)
        assert org_scope.verify_instance_view_access(HOST, "org-a", "same-name") == "org-a"
        assert org_scope.verify_instance_view_access(HOST, "org-b", "only-b") == "org-b"
        assert (
            org_scope.check_tenant_permission(
                HOST,
                "org-b",
                "only-b",
                "operate",
            )
            is False
        )
        with pytest.raises(HTTPException) as detail_denied:
            await instances.get_instance(
                "same-name",
                host=HOST,
                project="org-b",
                lxd_service=_NeverLXD(),
            )
        with pytest.raises(HTTPException) as metrics_denied:
            await instances.get_instance_metrics(
                "same-name",
                host=HOST,
                project="org-b",
                lxd_service=_NeverLXD(),
            )
        with pytest.raises(HTTPException) as console_denied:
            await console.check_active_sessions(
                "same-name",
                host=HOST,
                project="org-b",
                _rbac=_tenant(),
            )
        return (
            detail_denied.value.status_code,
            metrics_denied.value.status_code,
            console_denied.value.status_code,
        )

    try:
        assert asyncio.run(drive()) == (403, 403, 403)
    finally:
        session.close()
        engine.dispose()

    assert lxd_calls == []


def test_acl_filters_cannot_reopen_a_foreign_project_for_wildcard():
    org_scope.set_allowed_clusters({HOST})
    visible_token = org_scope._visible_projects_var.set({HOST: {"default", "org-a"}})
    acl_token = org_scope._tenant_acl_var.set({HOST: {("*", "*"): {"view"}}})
    try:
        assert org_scope.filter_projects_by_acl(HOST, ["default", "org-a", "org-b"]) == ["default", "org-a"]
        assert org_scope.filter_instances_by_acl(HOST, "org-b", ["same-name"]) == []
        assert org_scope.check_tenant_permission(HOST, "org-b", "same-name", "view") is False
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._visible_projects_var.reset(visible_token)
        org_scope.set_allowed_clusters(None)


def _vm(project: str, name: str = "same-name") -> VMInfo:
    return VMInfo(
        name=name,
        status=VMStatus.RUNNING,
        status_code=103,
        host=HOST,
        host_endpoint="https://control-plane.invalid:8443",
        project=project,
        type="virtual-machine",
        architecture="x86_64",
        config={},
        devices={},
        expanded_devices={},
        effective_limits_known=True,
        expanded_devices_known=True,
    )


class _Cache:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, namespace, key):
        return self.values.get((namespace, key))

    def set(self, namespace, key, value, ttl):
        self.values[(namespace, key)] = value
        self.writes.append((key, ttl))

    def instance_list_generation(self):
        return 0


class _Fleet:
    calls = 0

    async def list_all_vms_with_failures(self):
        self.calls += 1
        return [_vm("org-a"), _vm("org-b"), _vm("org-b", "only-b")], [HOST], []


def test_specifications_cache_hot_and_cold_keep_the_project_bound(monkeypatch):
    engine, session, db = _database()
    cache = _Cache()
    fleet = _Fleet()
    monkeypatch.setattr(instances, "get_cache_manager", lambda: cache)

    async def drive():
        await org_scope.org_scope_dependency(user=_tenant(), db=db)
        cold = await instances.list_instance_specifications(
            lxd_service=fleet,
            fresh=False,
        )
        hot = await instances.list_instance_specifications(
            lxd_service=fleet,
            fresh=False,
        )
        return cold, hot, org_scope.get_scope_cache_key("instances:specifications")

    try:
        cold, hot, cache_key = asyncio.run(drive())
    finally:
        session.close()
        engine.dispose()

    expected = [("org-a", "same-name"), ("org-b", "only-b")]
    assert [(row.project, row.name) for row in cold.instances] == expected
    assert [(row.project, row.name) for row in hot.instances] == expected
    assert fleet.calls == 1
    assert cache.writes == [(cache_key, 30)]
    assert cold.collected_at.tzinfo == timezone.utc


def test_tenant_self_view_reports_expanded_enforced_projects_only():
    engine, session, db = _database()

    async def drive():
        await org_scope.org_scope_dependency(user=_tenant(), db=db)
        return await tenant_access.get_my_access(user=_tenant(), db=db)

    try:
        response = asyncio.run(drive())
    finally:
        session.close()
        engine.dispose()

    assert [(entry.project_name, entry.instance_name, entry.policy_name) for entry in response.access] == [
        ("default", "*", "A wildcard"),
        ("org-a", "*", "A wildcard"),
        ("org-b", "only-b", "B narrow"),
    ]
    assert response.total_clusters == 1
    assert response.total_rules == 3
