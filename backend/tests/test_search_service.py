"""Unit tests for global-search orchestration: RBAC gating, degraded handling,
per-type caps and group ranking. Uses fake adapters + a no-op cache so the test
is DB/LXD-free (matches the rest of this suite)."""

import asyncio
import os
from types import SimpleNamespace

# database.py validates this at import (it does not connect); set a dummy so the
# search service module - which pulls in org_scope -> database - is importable.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/test")

from app.core.rbac import Role  # noqa: E402
from app.models.search_schemas import SearchResultItem  # noqa: E402
from app.services.search import service  # noqa: E402
from app.services.search.adapters import ResourceAdapter  # noqa: E402


class _NoCache:
    def get(self, *a, **k):
        return None

    def set(self, *a, **k):
        pass


# isolate orchestration from the real cache/redis layer
service.get_cache_manager = lambda: _NoCache()


class FakeAdapter(ResourceAdapter):
    def __init__(self, type_, label, names, min_role=Role.TENANT, fail=False):
        self.type = type_
        self.label = label
        self._names = names
        self.min_role = min_role
        self._fail = fail

    async def fetch(self, ctx):
        if self._fail:
            raise RuntimeError("source down")
        return [{"name": n} for n in self._names]

    def fields(self, c):
        return [(c["name"], 1.0, "name")]

    def to_item(self, c, score, field):
        return SearchResultItem(type=self.type, id=c["name"], title=c["name"], score=score, match_field=field)


_SETTINGS = SimpleNamespace(auth_mode="client-cert", audit_db_enabled=True)
_ADMIN = SimpleNamespace(user_id="admin-1", email="admin@test", groups=["role_admin"])
_TENANT = SimpleNamespace(user_id="tenant-1", email="tenant@test", groups=["role_tenant"])


def _run(user, adapters, q="web", types=None, limit=5):
    service.ADAPTERS = adapters
    return asyncio.run(service.global_search(q, types, limit, user, None, None, _SETTINGS))


def test_admin_sees_admin_only_group_tenant_does_not():
    adapters = [
        FakeAdapter("instance", "Instances", ["web-1", "web-2"]),
        FakeAdapter("audit", "Audit Log", ["web.delete"], min_role=Role.ADMIN),
    ]
    admin_types = {g.type for g in _run(_ADMIN, adapters).groups}
    assert "audit" in admin_types and "instance" in admin_types

    tenant_types = {g.type for g in _run(_TENANT, adapters).groups}
    assert "audit" not in tenant_types
    assert "instance" in tenant_types


def test_degraded_when_a_source_fails():
    adapters = [
        FakeAdapter("instance", "Instances", ["web-1"]),
        FakeAdapter("network", "Networks", [], fail=True),
    ]
    res = _run(_ADMIN, adapters)
    assert res.degraded is True
    assert any(g.type == "instance" for g in res.groups)  # healthy source still returns


def test_per_type_cap_reports_true_total():
    names = [f"web-{i}" for i in range(10)]
    res = _run(_ADMIN, [FakeAdapter("instance", "Instances", names)], limit=3)
    g = res.groups[0]
    assert len(g.items) == 3
    assert g.total == 10


def test_groups_ranked_by_best_score():
    adapters = [
        FakeAdapter("instance", "Instances", ["web-server"]),  # prefix match
        FakeAdapter("network", "Networks", ["web"]),  # exact match
    ]
    res = _run(_ADMIN, adapters, q="web")
    assert res.groups[0].type == "network"


def test_empty_query_returns_empty():
    res = _run(_ADMIN, [FakeAdapter("instance", "Instances", ["web-1"])], q="   ")
    assert res.total == 0 and res.groups == []


def test_type_filter_restricts_groups():
    adapters = [
        FakeAdapter("instance", "Instances", ["web-1"]),
        FakeAdapter("network", "Networks", ["web-net"]),
    ]
    res = _run(_ADMIN, adapters, types={"network"})
    assert {g.type for g in res.groups} == {"network"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} passed")
