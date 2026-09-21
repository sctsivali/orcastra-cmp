"""The second owner is admitted only after a complete, sanitized cluster audit."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.routers import cluster_registration, integrations
from app.services import shared_cluster_audit as guard
from app.services.organization_service import OrganizationService
from app.models.db_models import OrganizationCluster


class _Result:
    def __init__(self, rows: list[Any]):
        self.rows = rows

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self


class _Session:
    def __init__(self, results: list[list[Any]], dialect: str = "sqlite"):
        self.results = list(results)
        self.dialect = dialect
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self.deleted: list[Any] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return _Result(self.results.pop(0) if self.results else [])

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def flush(self):
        self.flushes += 1

    async def refresh(self, _value):
        return None

    async def delete(self, value):
        self.deleted.append(value)


def _report(*, safe: bool, complete: bool) -> guard.SharedClusterAuditReport:
    findings = ()
    if not safe:
        findings = (
            guard.SharedClusterFindingSummary(
                scope="profile",
                code="raw_config_forbidden",
                field="config.raw.*",
                count=1,
            ),
        )
    return guard.SharedClusterAuditReport(
        cluster_id="cluster-a",
        safe=safe,
        complete=complete,
        projects_checked=1 if complete else 0,
        profiles_checked=1 if complete else 0,
        instances_checked=1 if complete else 0,
        findings=findings,
    )


def test_first_or_existing_owner_needs_no_daemon_io(monkeypatch):
    async def must_not_run(_cluster_id: str):
        raise AssertionError("daemon audit must not run without a new owner identity")

    monkeypatch.setattr(guard, "audit_shared_cluster", must_not_run)

    async def exercise():
        first = _Session([[]])
        assert await guard.assert_cluster_safe_to_share(
            first,
            "cluster-a",
            prospective_organization_id=1,
        ) is None

        existing = _Session([[[1, 1, "owner@example.test"]]])
        assert await guard.assert_cluster_safe_to_share(
            existing,
            "cluster-a",
            prospective_organization_id=1,
        ) is None

    asyncio.run(exercise())


def test_clean_complete_inventory_allows_a_second_owner(monkeypatch):
    async def safe(_cluster_id: str):
        return _report(safe=True, complete=True)

    monkeypatch.setattr(guard, "audit_shared_cluster", safe)
    session = _Session([[[1, 1, "owner@example.test"]]])

    report = asyncio.run(
        guard.assert_cluster_safe_to_share(
            session,
            "cluster-a",
            prospective_organization_id=2,
        )
    )

    assert report is not None
    assert report.safe is True
    assert report.complete is True


def test_registration_refuses_a_foreign_existing_owner_before_daemon_io(monkeypatch):
    async def org_ids(_user, _db):
        return [2]

    async def must_not_run(_cluster_id: str):
        raise AssertionError("an unauthorized registration must not read the daemon")

    monkeypatch.setattr(org_scope, "get_user_org_ids", org_ids)
    monkeypatch.setattr(guard, "audit_shared_cluster", must_not_run)
    session = _Session([[[1, "first@example.test"]]])
    user = SimpleNamespace(
        email="second@example.test",
        user_id="second",
        groups=["partner"],
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            cluster_registration._guard_registration_binding(
                session,
                "cluster-a",
                user,
            )
        )

    assert getattr(caught.value, "status_code", None) == 403


def test_active_create_blocks_sharing_without_a_racy_daemon_snapshot(monkeypatch):
    async def must_not_run(_cluster_id: str):
        raise AssertionError("an active durable create must block before daemon audit")

    monkeypatch.setattr(guard, "audit_shared_cluster", must_not_run)
    session = _Session(
        [
            [[1, 1, "first@example.test"]],
            [99],
        ]
    )

    with pytest.raises(guard.SharedClusterGuardError) as caught:
        asyncio.run(
            guard.assert_cluster_safe_to_share(
                session,
                "cluster-a",
                prospective_organization_id=2,
            )
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "cluster_configuration_operation_active"
    assert caught.value.report.findings[0].code == "active_configuration_operation"


@pytest.mark.parametrize(
    ("report", "status_code", "code"),
    [
        (_report(safe=False, complete=True), 409, "cluster_not_safe_to_share"),
        (_report(safe=False, complete=False), 503, "cluster_share_audit_unavailable"),
    ],
)
def test_unsafe_partial_or_unavailable_inventory_fails_closed(
    monkeypatch,
    report,
    status_code,
    code,
):
    async def verdict(_cluster_id: str):
        return report

    monkeypatch.setattr(guard, "audit_shared_cluster", verdict)
    session = _Session([[[1, 1, "owner@example.test"]]])

    with pytest.raises(guard.SharedClusterGuardError) as caught:
        asyncio.run(
            guard.assert_cluster_safe_to_share(
                session,
                "cluster-a",
                prospective_organization_id=2,
            )
        )

    assert caught.value.status_code == status_code
    assert caught.value.as_detail()["code"] == code


class _Response:
    def __init__(self, rows):
        self.rows = rows

    def json(self):
        return {"metadata": self.rows}


class _Endpoint:
    def __init__(self, rows):
        self.rows = rows

    def get(self, **_kwargs):
        return _Response(self.rows)


class _API:
    def __init__(self):
        safe_project = {
            "name": "customer-project",
            "config": {
                "restricted": "true",
                "features.images": "true",
                "features.networks": "true",
                "features.profiles": "true",
                "features.storage.volumes": "true",
            },
        }
        self.projects = _Endpoint([safe_project])
        self.profiles = _Endpoint(
            [
                {
                    "name": "customer-profile",
                    "project": "customer-project",
                    "config": {"raw.lxc": "SECRET_PROFILE_VALUE"},
                    "devices": {"accelerator": {"type": "gpu", "id": "SECRET_GPU"}},
                }
            ]
        )
        instance = {
            "name": "customer-instance",
            "project": "customer-project",
            "config": {"user.user-data": "password: SECRET_CLOUD_INIT"},
            "devices": {"root": {"type": "disk", "path": "/"}},
            "expanded_config": {"user.user-data": "password: SECRET_CLOUD_INIT"},
            "expanded_devices": {"root": {"type": "disk", "path": "/"}},
        }
        self.instances = _Endpoint([instance])

    def __getitem__(self, item):
        return getattr(self, item)


def test_admin_audit_is_value_free_even_when_daemon_payload_contains_secrets():
    client = SimpleNamespace(
        api=_API(),
        host_info={
            "api_extensions": ["profiles_all_projects", "instance_all_projects"]
        },
    )
    service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda _cluster_id: client)
    )

    report = guard._scan_cluster_sync("cluster-a", service)
    encoded = json.dumps(report.as_dict())

    assert report.complete is True
    assert report.safe is False
    assert "raw_config_forbidden" in encoded
    assert "host_device_forbidden" in encoded
    for forbidden in (
        "SECRET_PROFILE_VALUE",
        "SECRET_GPU",
        "SECRET_CLOUD_INIT",
        "customer-profile",
        "customer-instance",
        "customer-project",
        "accelerator",
    ):
        assert forbidden not in encoded


def test_partial_daemon_inventory_is_reported_incomplete_without_raw_error():
    api = _API()
    api.instances = _Endpoint(["/1.0/instances/customer-secret"])
    client = SimpleNamespace(
        api=api,
        host_info={
            "api_extensions": ["profiles_all_projects", "instance_all_projects"]
        },
    )
    service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda _cluster_id: client)
    )

    report = asyncio.run(guard.audit_shared_cluster("cluster-a", lxd_service=service))
    encoded = json.dumps(report.as_dict())

    assert report.safe is False
    assert report.complete is False
    assert report.findings[0].code == "inventory_unavailable"
    assert "customer-secret" not in encoded


def test_postgres_uses_a_transaction_advisory_lock():
    session = _Session([[]], dialect="postgresql")

    async def exercise():
        async with guard.serialize_cluster_configuration(session, "cluster-a"):
            pass

    asyncio.run(exercise())
    assert "pg_advisory_xact_lock" in str(session.executed[0][0])


def test_all_four_binding_paths_enforce_a_clean_transition(monkeypatch):
    async def safe(_cluster_id: str):
        return _report(safe=True, complete=True)

    async def org_ids(_user, _db):
        return [2]

    async def ordered_org_ids(_email, _db):
        return [2]

    monkeypatch.setattr(guard, "audit_shared_cluster", safe)
    monkeypatch.setattr(org_scope, "get_user_org_ids", org_ids)
    monkeypatch.setattr(org_scope, "_ordered_org_ids_for_email", ordered_org_ids)

    async def exercise():
        # Manual bind: audit owner 1, then insert owner 2.
        manual = _Session(
            [
                [[1, 1, "first@example.test"]],
                [],
            ]
        )
        bound = await OrganizationService(manual).bind_cluster(
            2,
            "cluster-a",
            "second@example.test",
        )
        assert bound.organization_id == 2
        assert manual.commits == 1

        # Registration of an existing cluster is pre-bound before Vault is touched.
        registration = _Session(
            [
                [[1, "second@example.test"]],
                [[1, 1, "second@example.test"]],
            ]
        )
        async with guard.serialize_cluster_share(registration, "cluster-a"):
            ids = await cluster_registration._guard_registration_binding(
                registration,
                "cluster-a",
                SimpleNamespace(
                    email="second@example.test",
                    user_id="second",
                    groups=["partner"],
                ),
            )
        assert ids == [2]
        assert registration.commits == 0

        # External provisioning uses the same guard before its flush.
        provision = _Session(
            [
                [],
                [[1, 1, "first@example.test"]],
            ]
        )
        mapped = await integrations._ensure_provision_cluster_mapping(
            provision,
            organization_id=2,
            cluster_id="cluster-a",
            registered_by="second@example.test",
        )
        assert mapped is True
        assert provision.flushes == 1

        # Reconciliation treats the NULL owner as ambiguous and audits before re-parenting it.
        unbound = OrganizationCluster(
            id=7,
            organization_id=None,
            cluster_id="cluster-a",
            registered_by="second@example.test",
        )
        reconcile = _Session(
            [
                [unbound],
                [],
                [[7, None, "second@example.test"]],
            ]
        )
        count = await org_scope.reconcile_personal_clusters(
            reconcile,
            "second@example.test",
        )
        assert count == 1
        assert unbound.organization_id == 2
        assert reconcile.commits == 1

    asyncio.run(exercise())


def test_every_owner_transition_path_calls_the_same_guard():
    paths = {
        "manual bind": inspect.getsource(OrganizationService.bind_cluster),
        "registration auto-bind": (
            inspect.getsource(cluster_registration._guard_registration_binding)
            + inspect.getsource(cluster_registration.register_cluster)
        ),
        "external provision": inspect.getsource(
            integrations._ensure_provision_cluster_mapping
        ),
        "unbound reconcile": inspect.getsource(org_scope.reconcile_personal_clusters),
    }
    for label, source in paths.items():
        assert "assert_cluster_safe_to_share" in source, label
        assert "serialize_cluster_share" in source, label


def test_registration_guards_existing_ownership_before_vault_mutation():
    source = inspect.getsource(cluster_registration.register_cluster)
    assert source.index("_guard_registration_binding") < source.index(
        "save_cluster_metadata"
    )
