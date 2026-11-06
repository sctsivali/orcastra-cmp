"""A signed-in caller whose groups map to no Orcastra role is refused, not served an empty fleet.

Issue #17: Authentik signs in anyone it knows, and a caller with no role_* group used to resolve to
Tenant. Every scoped route then answered 200 with nothing in it, which reads as a broken or emptied
deployment rather than as "you have not been given access yet". Each RBAC dependency now refuses
such a caller with 403 and a machine-readable reason the dashboard turns into its access notice.

The refusal is pinned on all three dependencies and through the real application, so the
structured detail survives the error sanitizer on its way out.
"""

import asyncio
import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "0.0.0-test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

from app.core import rbac  # noqa: E402
from app.core.auth import AuthenticatedUser, get_current_user  # noqa: E402
from app.core.rbac import (  # noqa: E402
    NO_ROLE_ASSIGNED,
    Permission,
    Role,
    has_mapped_role,
    mapped_roles,
    require_authenticated,
    require_permissions,
    require_roles,
    resolve_roles,
)


def _user(*groups: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id="sub-1", email="someone@example.test", groups=list(groups))


def _request(path: str = "/api/v1/nodes") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


DEPENDENCIES = {
    "require_roles(tenant)": require_roles(Role.TENANT),
    "require_permissions(instance:read)": require_permissions(Permission.INSTANCE_READ),
    "require_authenticated()": require_authenticated(),
}


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    monkeypatch.setattr(rbac.activity_logger, "log_activity", lambda **kwargs: calls.append(kwargs))
    return calls


@pytest.mark.parametrize("name", sorted(DEPENDENCIES))
@pytest.mark.parametrize("groups", [(), ("Everyone",), ("authentik Read-only",), ("role_unknown",)])
def test_no_mapped_role_is_refused_with_a_reason(name: str, groups: tuple, audit: list) -> None:
    request = _request()
    with pytest.raises(HTTPException) as refused:
        asyncio.run(DEPENDENCIES[name](request=request, user=_user(*groups)))
    assert refused.value.status_code == 403
    assert refused.value.detail["error"] == NO_ROLE_ASSIGNED
    assert "administrator" in refused.value.detail["message"]
    assert request.state.user_role is None, "an access log must not record the Tenant default"
    assert [call["details"]["reason"] for call in audit] == [NO_ROLE_ASSIGNED]
    assert audit[0]["action"] == "rbac.access_denied"
    assert audit[0]["target_id"] == "/api/v1/nodes"


@pytest.mark.parametrize("name", sorted(DEPENDENCIES))
@pytest.mark.parametrize("group", ["role_tenant", "tenant", "orcastra-tenants", "ROLE_TENANT "])
def test_any_tenant_alias_is_a_role(name: str, group: str, audit: list) -> None:
    user = _user("Everyone", group)
    assert asyncio.run(DEPENDENCIES[name](request=_request(), user=user)) is user
    assert audit == []


def test_dev_mode_still_answers_the_fabricated_admin(audit: list) -> None:
    for dependency in DEPENDENCIES.values():
        user = asyncio.run(dependency(request=_request(), user=None))
        assert user.groups == ["admin"]
    assert audit == []


def test_the_tenant_default_is_unchanged_for_everything_but_sessions() -> None:
    # Tenant grants, the integration rebind and the role reader resolve through resolve_roles, and
    # a key or a membership row is not a session, so they keep reading a role-less caller as Tenant.
    assert resolve_roles(_user()) == [Role.TENANT]
    assert mapped_roles([]) == []
    assert mapped_roles(["Everyone", "role_partner"]) == [Role.PARTNER]
    assert has_mapped_role(_user("authentik admins")) is True
    assert has_mapped_role(_user("Everyone")) is False


@pytest.fixture(scope="module")
def client():
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user("Everyone")
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/system/settings"),
        ("GET", "/api/v1/user-preferences"),
        ("POST", "/api/v1/ws-tickets"),
        ("GET", "/api/v1/organizations"),
        ("GET", "/api/v1/tenant-access/my-access"),
    ],
)
def test_the_application_answers_the_structured_refusal(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == NO_ROLE_ASSIGNED
