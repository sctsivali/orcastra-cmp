"""Only Partners and Admins read host kernel advisories, and only for clusters in their scope.

Host kernels are host facts: a Tenant has no business with them, and a Partner asking for a
cluster outside its organization is refused before any cluster is touched. A live read of
Ubuntu's records (POST .../check) reaches the internet on the deployment's behalf, so only an
Administrator may ask for it.
"""

from typing import Any, List

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.models.kernel_advisory_schemas import KernelAdvisoryResponse
from app.routers import security_advisories

PATH = "/security/kernel-advisories"


@pytest.fixture
def built(monkeypatch) -> List[Any]:
    calls: List[Any] = []

    async def fake_build(_service, *, host, fresh, wall, today=None):
        calls.append((host, fresh, wall))
        return KernelAdvisoryResponse()

    monkeypatch.setattr(security_advisories, "build_kernel_report", fake_build)
    return calls


def _client(groups: List[str]) -> TestClient:
    from app.core.auth import AuthenticatedUser
    from app.core.rbac import get_current_user

    app = FastAPI()
    app.include_router(security_advisories.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=groups[0], email=f"{groups[0]}@example.test", groups=groups
    )
    app.dependency_overrides[security_advisories.org_scope_dependency] = lambda: None
    app.dependency_overrides[security_advisories.get_lxd_service] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def test_a_tenant_is_refused(built):
    response = _client(["role_tenant"]).get(PATH)
    assert response.status_code == 403, response.text[:200]
    assert built == []


@pytest.mark.parametrize("groups", [["role_partner"], ["role_admin"]])
def test_a_partner_and_an_admin_are_served(built, groups):
    response = _client(groups).get(PATH)
    assert response.status_code == 200, response.text[:200]
    body = response.json()
    assert body["hosts"] == [] and body["counts"] == {"exposed": 0, "unverified": 0, "patched": 0}
    assert built == [(None, False, security_advisories.KERNEL_ADVISORY_WALL_SECONDS)]


def test_fresh_is_passed_through(built):
    _client(["role_admin"]).get(PATH, params={"fresh": "true"})
    assert built[-1][1] is True


def test_a_cluster_outside_the_scope_is_refused_before_any_read(built, monkeypatch):
    def refuse(host):
        raise HTTPException(status_code=403, detail=f"Access denied to cluster {host}")

    monkeypatch.setattr(security_advisories, "verify_cluster_access", refuse)
    response = _client(["role_partner"]).get(PATH, params={"host": "someone-elses"})
    assert response.status_code == 403
    assert built == []


def test_a_failed_build_is_a_500_without_internals(monkeypatch):
    async def broken(*_a, **_k):
        raise RuntimeError("vault said no at /secret/data/clusters")

    monkeypatch.setattr(security_advisories, "build_kernel_report", broken)
    response = _client(["role_admin"]).get(PATH)
    assert response.status_code == 500
    assert "vault" not in response.text


@pytest.fixture
def feed(monkeypatch) -> List[Any]:
    """The Ubuntu check with no network: a stored copy only, a check that records the call."""
    from app.services.kernel_cve_feed import service
    from app.services.kernel_cve_feed.store import KernelCveFeedStore

    checks: List[Any] = []
    store = KernelCveFeedStore(redis=lambda: None)

    async def fake_check(cve_ids=None, now=None):
        checks.append(cve_ids)
        return {}

    class _Quiet:
        def log_activity(self, **_kwargs):
            return None

        def invalidate_resource_cache(self, *_kinds):
            return None

    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(service, "check", fake_check)
    monkeypatch.setattr(security_advisories, "get_activity_logger", lambda: _Quiet())
    monkeypatch.setattr(security_advisories, "get_cache_manager", lambda: _Quiet())
    return checks


@pytest.mark.parametrize("groups, expected", [(["role_tenant"], 403), (["role_partner"], 403), (["role_admin"], 200)])
def test_only_an_administrator_checks_ubuntu_now(feed, groups, expected):
    response = _client(groups).post(f"{PATH}/check")
    assert response.status_code == expected, response.text[:200]
    assert feed == ([None] if expected == 200 else [])


@pytest.mark.parametrize("groups, expected", [(["role_tenant"], 403), (["role_partner"], 200), (["role_admin"], 200)])
def test_partners_and_admins_read_when_ubuntu_was_checked(feed, groups, expected):
    response = _client(groups).get(f"{PATH}/ubuntu-status")
    assert response.status_code == expected, response.text[:200]
    if expected == 200:
        body = response.json()
        assert [a["id"] for a in body["advisories"]] == ["CVE-2026-99999"]
        assert body["advisories"][0]["ubuntu_url"] == "https://ubuntu.com/security/CVE-2026-99999"
    assert feed == []
