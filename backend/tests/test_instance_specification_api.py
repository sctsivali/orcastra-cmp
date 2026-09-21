"""HTTP contract checks for typed instance specification reads and writes."""

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.auth import AuthenticatedUser, get_current_user
from app.core import rbac
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import Role
from app.routers import instance_specifications as router_module
from app.services.instance_specifications import SpecificationRead, request_hash


PLAN_REVISION = "a" * 64


def _public_specification() -> dict:
    return {
        "instance_name": "web-1",
        "host": "cluster-a",
        "project": "project-a",
        "type": "container",
        "status": "Running",
        "cpu": {"value": 2, "source": "profile"},
        "memory": {"value_bytes": 2 * 1024**3, "source": "profile"},
        "root_disk": {
            "value_bytes": 10 * 1024**3,
            "pool": "fast",
            "source": "profile",
            "editable": True,
            "reason": None,
        },
        "capabilities": {"live_update": True, "restart_supported": True},
        "etag": '"v1"',
    }


def _current() -> SpecificationRead:
    return SpecificationRead(
        public=_public_specification(),
        raw={},
        etag='"v1"',
        root_device_name="root",
    )


def _plan(*, revision: str = PLAN_REVISION) -> dict:
    return {
        "instance_name": "web-1",
        "host": "cluster-a",
        "project": "project-a",
        "status": "Running",
        "type": "container",
        "etag": '"v1"',
        "plan_revision": revision,
        "execution": "live",
        "changes": [
            {
                "field": "cpu_count",
                "before": 2,
                "after": 4,
                "source": "profile",
            }
        ],
        "warnings": [],
        "blockers": [],
        "required_acknowledgements": [],
    }


def _job(*, kind: str = "specification", payload_hash: str = "hash") -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        job_id="job-1",
        kind=kind,
        instance_name="web-1",
        cluster_id="cluster-a",
        project="project-a",
        phase="pending",
        status="pending",
        operation_id=None,
        error=None,
        result=None,
        verified_spec=None,
        created_at=now,
        updated_at=now,
        payload_hash=payload_hash,
    )


def _app(monkeypatch, groups: list[str]) -> FastAPI:
    app = FastAPI()
    app.include_router(router_module.router)
    user = AuthenticatedUser(
        user_id=f"user-{'-'.join(groups)}",
        email="user@example.test",
        groups=groups,
    )

    async def current_user() -> AuthenticatedUser:
        return user

    async def no_scope() -> None:
        return None

    async def writable() -> None:
        return None

    async def database() -> object:
        return object()

    async def target_organization(*args, **kwargs):
        return SimpleNamespace(
            organization_id=17,
            organization_slug="org-seventeen",
            resolution="exact",
        )

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[org_scope_dependency] = no_scope
    app.dependency_overrides[check_read_only_mode] = writable
    app.dependency_overrides[get_db] = database
    role = Role.PARTNER if "role_partner" in groups else Role.TENANT
    monkeypatch.setattr(rbac, "resolve_roles", lambda _user: [role])
    monkeypatch.setattr(
        router_module,
        "verify_instance_view_access",
        lambda host, project, instance_name: project,
    )
    monkeypatch.setattr(
        router_module,
        "resolve_instance_job_organization",
        target_organization,
    )
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _apply_body(**overrides) -> dict:
    body = {
        "host": "cluster-a",
        "project": "project-a",
        "cpu_count": 4,
        "plan_revision": PLAN_REVISION,
        "apply_mode": "live_only",
        "acknowledgements": [],
    }
    body.update(overrides)
    return body


async def test_tenant_can_read_safe_specification_but_cannot_plan_or_apply(
    monkeypatch,
) -> None:
    calls = []

    async def read(*args):
        calls.append(args)
        return _current()

    async def preflight(*args):
        raise AssertionError("tenant edit preflight reached daemon service")

    monkeypatch.setattr(router_module, "read_specification", read)
    monkeypatch.setattr(router_module, "preflight_specification", preflight)
    app = _app(monkeypatch, ["role_tenant"])

    async with _client(app) as client:
        response = await client.get(
            "/instances/web-1/specification",
            params={"host": "cluster-a", "project": "project-a"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["etag"] == '"v1"'
        assert response.json()["cpu"] == {"value": 2, "source": "profile"}
        assert calls == [("web-1", "cluster-a", "project-a")]

        response = await client.post(
            "/instances/web-1/specification/preflight",
            json={"host": "cluster-a", "project": "project-a", "cpu_count": 4},
        )
        assert response.status_code == 403

        response = await client.patch(
            "/instances/web-1/specification",
            json=_apply_body(),
            headers={"If-Match": '"v1"', "Idempotency-Key": "tenant-attempt"},
        )
        assert response.status_code == 403


async def test_tenant_instance_acl_denial_happens_before_specification_daemon_io(
    monkeypatch,
) -> None:
    from fastapi import HTTPException

    daemon_reads = []

    async def must_not_read(*args):
        daemon_reads.append(args)
        raise AssertionError("an unauthorized instance reached the daemon")

    async def load_job(_job_id):
        return _job()

    def deny_exact_instance(host, project, instance_name):
        assert (host, project, instance_name) == (
            "cluster-a",
            "project-a",
            "web-1",
        )
        raise HTTPException(status_code=404, detail="Instance not found")

    monkeypatch.setattr(router_module, "read_specification", must_not_read)
    monkeypatch.setattr(router_module, "get_operation_job", load_job)
    app = _app(monkeypatch, ["role_tenant"])
    monkeypatch.setattr(
        router_module,
        "verify_instance_view_access",
        deny_exact_instance,
    )

    async with _client(app) as client:
        specification = await client.get(
            "/instances/web-1/specification",
            params={"host": "cluster-a", "project": "project-a"},
        )
        operation = await client.get("/instances/operations/job-1")

    assert specification.status_code == 404
    assert operation.status_code == 404
    assert daemon_reads == []


async def test_preflight_is_strict_and_passes_only_typed_changes(monkeypatch) -> None:
    calls = []

    async def preflight(*args):
        calls.append(args)
        return _current(), _plan()

    monkeypatch.setattr(router_module, "preflight_specification", preflight)
    app = _app(monkeypatch, ["role_partner"])

    async with _client(app) as client:
        response = await client.post(
            "/instances/web-1/specification/preflight",
            json={"host": "cluster-a", "project": "project-a", "cpu_count": 4},
        )
        assert response.status_code == 200, response.text
        assert calls == [("web-1", "cluster-a", "project-a", {"cpu_count": 4})]

        string_value = await client.post(
            "/instances/web-1/specification/preflight",
            json={"host": "cluster-a", "project": "project-a", "cpu_count": "4"},
        )
        assert string_value.status_code == 422
        arbitrary_map = await client.post(
            "/instances/web-1/specification/preflight",
            json={
                "host": "cluster-a",
                "project": "project-a",
                "cpu_count": 4,
                "config": {"raw.lxc": "lxc.mount.auto=proc:rw"},
            },
        )
        assert arbitrary_map.status_code == 422
    assert len(calls) == 1


async def test_apply_refuses_stale_etag_before_creating_a_job(monkeypatch) -> None:
    created = []

    async def preflight(*args):
        return _current(), _plan()

    async def no_replay(*args):
        return None

    async def create_job(**kwargs):
        created.append(kwargs)
        return _job(), True

    monkeypatch.setattr(router_module, "preflight_specification", preflight)
    monkeypatch.setattr(router_module, "get_operation_job_by_request", no_replay)
    monkeypatch.setattr(router_module, "create_operation_job", create_job)
    app = _app(monkeypatch, ["role_partner"])

    async with _client(app) as client:
        response = await client.patch(
            "/instances/web-1/specification",
            json=_apply_body(),
            headers={"If-Match": '"stale"', "Idempotency-Key": "stale-etag"},
        )
    assert response.status_code == 412, response.text
    assert created == []


async def test_apply_accepts_one_durable_job_and_returns_202(monkeypatch) -> None:
    accepted = []
    started = []
    drained = []

    async def preflight(*args):
        return _current(), _plan()

    async def no_replay(*args):
        return None

    async def create_job(**kwargs):
        accepted.append(kwargs)
        digest = request_hash(
            host="cluster-a",
            project="project-a",
            instance_name="web-1",
            requested={"cpu_count": 4},
            plan_revision=PLAN_REVISION,
            apply_mode="live_only",
            acknowledgements=[],
        )
        return _job(payload_hash=digest), True

    def run_job(job_id):
        started.append(job_id)

        async def finish() -> None:
            return None

        return finish()

    monkeypatch.setattr(router_module, "preflight_specification", preflight)
    monkeypatch.setattr(router_module, "get_operation_job_by_request", no_replay)
    monkeypatch.setattr(router_module, "create_operation_job", create_job)
    monkeypatch.setattr(router_module, "run_specification_job", run_job)

    async def drain(**kwargs):
        drained.append(kwargs)
        return 1

    monkeypatch.setattr(router_module, "drain_instance_operation_audits", drain)
    app = _app(monkeypatch, ["role_partner"])

    async with _client(app) as client:
        response = await client.patch(
            "/instances/web-1/specification",
            json=_apply_body(),
            headers={"If-Match": 'W/"v1"', "Idempotency-Key": "apply-once"},
        )
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "job-1"
    assert accepted[0]["requested"] == {"cpu_count": 4}
    assert accepted[0]["target_organization_id"] == 17
    assert accepted[0]["target_organization_resolution"] == "exact"
    assert drained == [{"job_id": "job-1"}]
    assert started == ["job-1"]


async def test_idempotency_key_cannot_replay_a_create_job(monkeypatch) -> None:
    requested = {"cpu_count": 4}
    digest = request_hash(
        host="cluster-a",
        project="project-a",
        instance_name="web-1",
        requested=requested,
        plan_revision=PLAN_REVISION,
        apply_mode="live_only",
        acknowledgements=[],
    )

    async def replay(*args):
        return _job(kind="create", payload_hash=digest)

    async def preflight(*args):
        raise AssertionError("mismatched replay reached daemon service")

    monkeypatch.setattr(router_module, "get_operation_job_by_request", replay)
    monkeypatch.setattr(router_module, "preflight_specification", preflight)
    app = _app(monkeypatch, ["role_partner"])

    async with _client(app) as client:
        response = await client.patch(
            "/instances/web-1/specification",
            json=_apply_body(),
            headers={"If-Match": '"v1"', "Idempotency-Key": "cross-kind"},
        )
    assert response.status_code == 409, response.text


async def test_operation_scope_is_checked_from_the_persisted_identity(monkeypatch) -> None:
    async def load(_job_id):
        return _job()

    checked = []

    def deny(host, project, instance_name):
        checked.append((host, project, instance_name))
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Instance operation not found")

    monkeypatch.setattr(router_module, "get_operation_job", load)
    app = _app(monkeypatch, ["role_tenant"])
    monkeypatch.setattr(router_module, "verify_instance_view_access", deny)

    async with _client(app) as client:
        response = await client.get("/instances/operations/job-1")
    assert response.status_code == 404
    assert checked == [("cluster-a", "project-a", "web-1")]
