"""The cluster members, operations, server, warnings and my-keys routers are partner-only.

Each of these routers accepted a tenant at the router level. Their pages are partner pages, and
what they return is cluster-level (member roles and addresses, LXD operations, server config,
daemon warnings, LXD client certificates), so a tenant could read it by calling the API
directly. The routers now require a partner, and the frontend stops calling them for a tenant,
which the structural checks at the bottom pin.
"""

import asyncio
import importlib
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.auth import AuthenticatedUser

MODULES = ["cluster_members", "lxd_operations", "lxd_server", "lxd_warnings", "my_keys"]

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )


def _user(role: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{role}-cluster-pages-probe",
        email=f"{role}@example.test",
        groups=[f"role_{role}"],
    )


def _router_gate(module_name: str):
    module = importlib.import_module(f"app.routers.{module_name}")
    return module.router.dependencies[0].dependency


@pytest.mark.parametrize("module_name", MODULES)
def test_a_tenant_is_refused_at_the_router(module_name: str) -> None:
    gate = _router_gate(module_name)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(gate(request=_request(f"/{module_name}"), user=_user("tenant")))
    assert raised.value.status_code == 403
    assert raised.value.detail == "Insufficient permissions"


@pytest.mark.parametrize("module_name", MODULES)
@pytest.mark.parametrize("role", ["partner", "admin"])
def test_a_partner_and_an_admin_pass_the_router(module_name: str, role: str) -> None:
    gate = _router_gate(module_name)
    user = _user(role)
    assert asyncio.run(gate(request=_request(f"/{module_name}"), user=user)) is user


# -- the frontend does not call these for a tenant ----------------------------------------------


def _function_body(source: str, name: str) -> str:
    start = source.index(f"export function {name}(")
    following = source.find("\nexport function ", start + 1)
    return source[start : following if following != -1 else len(source)]


@pytest.mark.parametrize("hook", ["useWarnings", "useSystemWarnings", "useServerInfo"])
def test_the_partner_only_hooks_are_disabled_for_a_tenant(hook: str) -> None:
    source = (FRONTEND / "hooks" / "use-lxd.ts").read_text(encoding="utf-8")
    body = _function_body(source, hook)
    assert "usePermissions()" in body
    assert "enabled: isPartner" in body, (
        f"{hook} runs for every role. Its backend router is partner-only, so every tenant page "
        "that mounts it fires a forbidden request"
    )


def test_the_my_keys_query_is_disabled_for_a_tenant() -> None:
    source = (FRONTEND / "app" / "my-keys" / "page.tsx").read_text(encoding="utf-8")
    query = source[source.index('queryKey: ["my-keys"]') :]
    query = query[: query.index("});")]
    assert "enabled: isPartner" in query


def test_the_alarm_statistics_card_is_hidden_from_a_tenant() -> None:
    source = (FRONTEND / "app" / "inventory" / "monitoring" / "monitoring-content.tsx").read_text(
        encoding="utf-8"
    )
    marker = source.index("{/* Alarm Statistics")
    card = source[marker : source.index("Alarm Statistics\n", marker)]
    assert re.search(r"\{isPartner && \(\s*<Card>", card), (
        "the Alarm Statistics card renders for a tenant, whose warnings query is disabled, so "
        "it would read a false zero"
    )
