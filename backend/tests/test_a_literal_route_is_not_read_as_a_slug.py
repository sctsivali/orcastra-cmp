"""Three routers share the /extensions prefix, and one of them owns /extensions/{slug}.

That path matches any single segment, so every literal route on the same prefix has to be
registered ahead of it. In the wrong order the failures are quiet and misleading:

  * `GET /extensions/self` is how an add-on reconciles. Matched by the slug route, it meets a
    partner JWT dependency it cannot satisfy and answers 401, which reads as a broken credential.
  * `GET /extensions/installations` would be read as an add-on whose slug is "installations" and
    answer 404, which reads as an empty list.

The router order in `main.py` prevents this, the reserved-slug list stops an author claiming one
of those names, and this file checks both, because an `include_router` line is easy to move.
"""

import os

import pytest
from starlette.routing import compile_path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "0.0.0-test")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

from app.core.extension_manifest import RESERVED_SLUGS  # noqa: E402
from app.core.public_openapi import _walk_route_paths  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return create_app()


def _resolve(app, method: str, path: str) -> str:
    """The name of the route the application would dispatch `method path` to.

    important: match on the SERVED path from `_walk_route_paths`, not on `route.matches`.
    From FastAPI 0.141 `include_router` stores a nested router whose routes carry only their
    own prefix, so `route.matches` finds nothing for `/api/v1/extensions/self`, while 0.128
    matches. requirements.txt allows both shapes. The walk keeps declaration order, which is
    what lets the literal route win over the slug.
    """
    for served, route in _walk_route_paths(app):
        if method not in (route.methods or set()):
            continue
        regex, _format, _convertors = compile_path(served)
        if regex.match(path):
            return route.name
    raise AssertionError(f"nothing matched {method} {path}")


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/v1/extensions/self", "describe_self"),
        ("GET", "/api/v1/extensions/vocabulary", "get_vocabulary"),
        ("GET", "/api/v1/extensions/installations", "list_installations"),
        ("GET", "/api/v1/extensions/installations/5", "get_installation"),
        ("PUT", "/api/v1/extensions/installations/5/scope", "rescope"),
        ("DELETE", "/api/v1/extensions/installations/5", "uninstall"),
        ("GET", "/api/v1/extensions/acme-backup", "get_extension"),
        ("GET", "/api/v1/extensions/acme-backup/install-preview", "install_preview"),
        ("POST", "/api/v1/extensions/acme-backup/installations", "install"),
        # The task journal's queue is a literal segment beside the {task_id} slug.
        ("GET", "/api/v1/tasks/queue", "get_queue"),
        ("GET", "/api/v1/tasks/9f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f", "get_task"),
    ],
)
def test_a_literal_route_wins_over_the_slug_route(app, method, path, expected) -> None:
    assert _resolve(app, method, path) == expected


def test_every_literal_first_segment_is_a_reserved_slug(app) -> None:
    """The reserved-slug guard, checked against the served routes.

    An author who could register the slug `installations` would be publishing an add-on whose
    detail page is a route the platform already answers on.
    """
    literals = set()
    for served, _route in _walk_route_paths(app):
        if not served.startswith("/api/v1/extensions/"):
            continue
        segment = served[len("/api/v1/extensions/"):].split("/")[0]
        if segment and not segment.startswith("{"):
            literals.add(segment)

    # important: the positive twin. A walk that misses the nested routers (FastAPI 0.141) finds
    # no path under `/api/v1/extensions/`, and the check below would pass on an empty set.
    assert {"self", "vocabulary", "installations"} <= literals, (
        f"the walk found {sorted(literals)}: it is not seeing the served extension routes"
    )
    unreserved = sorted(literals - RESERVED_SLUGS)
    assert not unreserved, (
        f"these route segments can be claimed as an add-on slug: {unreserved}. "
        "Add them to RESERVED_SLUGS, or an author shadows a platform route."
    )
