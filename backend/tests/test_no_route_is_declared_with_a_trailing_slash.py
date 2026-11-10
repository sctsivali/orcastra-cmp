"""No route is declared with a trailing slash.

FastAPI answers the other spelling with a 307 to the declared one. A browser replays a POST body on
that redirect, but the frontend's /api/v1 proxy streams the body upstream and undici cannot replay a
stream, so a POST through the app answers 502 "Backend unavailable" while the backend itself is
fine. Every client calls the bare form, so the bare form is the one declared.
"""

import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "0.0.0-test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

_ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}


def _declared_paths(source: str):
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)):
                continue
            if decorator.func.attr not in _ROUTE_METHODS or not decorator.args:
                continue
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                yield node.name, decorator.lineno, first.value


def test_no_router_declares_a_path_ending_in_a_slash() -> None:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        for function, line, route in _declared_paths(path.read_text()):
            # The application root has no prefix, so "/" is its only spelling.
            if path.name == "main.py" and route == "/":
                continue
            if route.endswith("/"):
                offenders.append(f"{path.relative_to(app_dir)}:{line} {function} {route!r}")
    assert not offenders, "declare these without the trailing slash:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/organizations"),
        ("GET", "/api/v1/organizations"),
        ("GET", "/api/v1/user-groups"),
    ],
)
def test_the_bare_path_reaches_its_route_without_a_redirect(method: str, path: str) -> None:
    from fastapi.testclient import TestClient

    from app.core.auth import AuthenticatedUser, get_current_user
    from app.main import create_app

    app = create_app()
    # A caller with no role stops at the route's own RBAC dependency, so a 403 proves the route ran.
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="sub-1", email="someone@example.test", groups=["Everyone"]
    )
    with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as client:
        response = client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 403, (response.status_code, response.headers.get("location"))


def test_the_guard_sees_a_slash_route() -> None:
    sample = '@router.post("/", status_code=201)\nasync def create():\n    pass\n'
    assert [route for _fn, _line, route in _declared_paths(sample)] == ["/"]
