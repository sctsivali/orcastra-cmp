"""Fakes for the project grant tests: a session that answers by table and records writes."""

from contextlib import asynccontextmanager
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser
from app.core.rbac import get_highest_role, resolve_roles
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy


class _Rows:
    def __init__(self, rows: List[Any]):
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self) -> List[Any]:
        return list(self._rows)


class Policy:
    _ids = 100

    def __init__(self, organization_id: int, name: str, source: str = "local", id: Optional[int] = None):
        self.id = id
        self.organization_id = organization_id
        self.name = name
        self.source = source


class Rule:
    # Ids well above the policy ids the tests pick, which the fake matches statement ints against.
    _ids = 9000

    def __init__(self, policy_id: int, cluster_id: str, project_name: str, instance_name: str = "*"):
        Rule._ids += 1
        self.id = Rule._ids
        self.policy_id = policy_id
        self.cluster_id = cluster_id
        self.project_name = project_name
        self.instance_name = instance_name
        self.permissions = ["view"]


class GrantSession:
    """Answers `execute` by the table the statement reads and remembers every write.

    The grant asks for one policy by (org, name) and one rule by (policy, cluster, project,
    instance); the revoke asks for every rule by (cluster, project), then each touched policy
    by id, its rules and its assignments. Bound values are read from the compiled statement.
    """

    def __init__(self, policies=(), rules=(), assignments=()):
        self.policies = list(policies)
        self.rules = list(rules)
        self.assignments = list(assignments)
        self.added: List[Any] = []
        self.deleted: List[Any] = []
        self.commits = 0
        self.flushes = 0

    @staticmethod
    def _params(stmt) -> dict:
        try:
            return dict(stmt.compile().params)
        except Exception:  # noqa: BLE001 - a statement with nothing bound
            return {}

    async def execute(self, stmt):
        sql = str(stmt)
        params = self._params(stmt)
        values = list(params.values())
        if "tenant_policy_assignments" in sql:
            policy_id = next((v for v in values if isinstance(v, int)), None)
            return _Rows([a for a in self.assignments if a.policy_id == policy_id])
        if "tenant_policy_rules" in sql:
            strings = [v for v in values if isinstance(v, str)]
            ints = [v for v in values if isinstance(v, int)]
            rows = [
                r for r in self.rules
                if (not ints or r.policy_id in ints)
                and all(s in (r.cluster_id, r.project_name, r.instance_name) for s in strings)
            ]
            # `select(TenantPolicyRule.id)` (rule_ids_naming) reads ids, as Postgres answers it.
            columns = list(getattr(stmt, "selected_columns", []))
            if len(columns) == 1 and columns[0].key == "id":
                return _Rows([r.id for r in rows])
            return _Rows(rows)
        if "tenant_access_policies" in sql:
            strings = [v for v in values if isinstance(v, str)]
            ints = [v for v in values if isinstance(v, int)]
            rows = [
                p for p in self.policies
                if (not strings or p.name in strings) and (not ints or p.id in ints or p.organization_id in ints)
            ]
            return _Rows(rows)
        raise AssertionError(f"unexpected query: {sql[:100]}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, __import__("app.models.db_models", fromlist=["TenantAccessPolicy"]).TenantAccessPolicy):
            self.policies.append(obj)
        else:
            self.rules.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None and hasattr(obj, "organization_id"):
                Policy._ids += 1
                obj.id = Policy._ids

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)
        if obj in self.rules:
            self.rules.remove(obj)
        if obj in self.policies:
            self.policies.remove(obj)

    async def commit(self) -> None:
        self.commits += 1


def partner(email: str = "p@example.test") -> AuthenticatedUser:
    return AuthenticatedUser(user_id="qa-partner", email=email, groups=["role_partner"], roles=["role_partner"])


def admin() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="qa-admin", email="a@example.test", groups=["role_admin"], roles=["role_admin"])


class NoCache:
    def __getattr__(self, name):
        return lambda *a, **k: None


def projects_client(service, monkeypatch, session, user=None) -> TestClient:
    """The projects router with LXD, auth, scope and cache stubbed; `session` is the db."""
    from app.core.database import get_db
    from app.core.dependencies import check_read_only_mode
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import projects as projects_module
    from app.services.lxd_service import get_lxd_service
    from app.services.webhooks import emitter

    monkeypatch.setattr(projects_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(projects_module, "get_cache_manager", lambda: NoCache())
    monkeypatch.setattr(projects_module.activity_logger, "log_activity", lambda **k: None, raising=False)

    async def _no_event(**_kwargs):
        return None

    monkeypatch.setattr(emitter, "emit_resource_event", _no_event)

    async def _write_policy(actor, host, db):
        role = get_highest_role(resolve_roles(actor))
        return ClusterWritePolicy(host, role, ClusterOwnership.EXCLUSIVE)

    @asynccontextmanager
    async def _configuration_lock(*args, **kwargs):
        yield

    monkeypatch.setattr(projects_module, "resolve_cluster_write_policy", _write_policy)
    monkeypatch.setattr(
        projects_module,
        "serialize_cluster_configuration",
        _configuration_lock,
    )

    async def _db():
        yield session

    app = FastAPI()
    app.include_router(projects_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    app.dependency_overrides[get_db] = _db
    client = TestClient(app, raise_server_exceptions=False)
    # A delete writes its follow-up row before the DELETE goes out (services/project_delete_store.py).
    from tests._project_delete_fakes import durable_deletes

    client.deletes = durable_deletes(monkeypatch)
    return client
