"""Every route a dashboard session reaches passes through a role check, except the one that says why.

rbac.refuse_unmapped lives in require_roles, require_permissions and require_authenticated. A route
that reads get_current_user without any of them would answer a caller with no role. Selection is by
dependant tree, so a new route is covered the moment it is added. Sockets identify their caller
through ws_user, which refuses the same caller.
"""

import os
from typing import Any, Iterator, Set, Tuple

import pytest
from fastapi.routing import APIWebSocketRoute

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "0.0.0-test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

from app.core.public_openapi import _walk_route_paths  # noqa: E402
from app.main import create_app  # noqa: E402

GUARDS = {
    "require_roles.<locals>._dependency",
    "require_permissions.<locals>._dependency",
    "require_authenticated.<locals>._dependency",
}
# The caller's own access verdict: the notice has to be able to ask it with no role.
UNGUARDED = {("GET", "/api/v1/me/access")}


def _calls(dependant: Any) -> Set[str]:
    names: Set[str] = set()
    stack = list(dependant.dependencies)
    while stack:
        dependency = stack.pop()
        call = getattr(dependency, "call", None)
        if call is not None:
            names.add(getattr(call, "__qualname__", getattr(call, "__name__", "")))
        stack.extend(dependency.dependencies)
    return names


def _sockets(container: Any, prefix: str = "", depth: int = 0) -> Iterator[Tuple[str, APIWebSocketRoute]]:
    for route in getattr(container, "routes", []):
        if isinstance(route, APIWebSocketRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            context = getattr(route, "include_context", None)
            yield from _sockets(route.original_router, prefix + str(getattr(context, "prefix", "") or ""), depth + 1)
        elif hasattr(route, "routes") and depth < 8:
            yield from _sockets(route, prefix, depth + 1)


@pytest.fixture(scope="module")
def app():
    return create_app()


def test_every_session_route_checks_the_role(app) -> None:
    unguarded = set()
    for path, route in _walk_route_paths(app):
        calls = _calls(route.dependant)
        if "get_current_user" in calls and not calls & GUARDS:
            unguarded |= {(method, path) for method in route.methods}
    assert unguarded == UNGUARDED


def test_every_socket_identifies_through_ws_user(app) -> None:
    sockets = dict(_sockets(app))
    assert len(sockets) >= 5, "the socket walk found too few routes to mean anything"
    for path, route in sockets.items():
        assert "ws_user.<locals>.dependency" in _calls(route.dependant), path
