"""Three routers share the /extensions prefix, and one of them owns /extensions/{slug}.

That path matches any single segment, so every literal route on the same prefix has to be
registered ahead of it. Registered the other way round the failures are quiet and misleading
rather than loud:

  * `GET /extensions/self` is how an add-on reconciles. Matched by the slug route, it meets a
    partner JWT dependency it has no way to satisfy and answers 401, which reads to the author as
    a broken credential rather than as a routing mistake on our side.
  * `GET /extensions/installations` would be read as an add-on whose slug is "installations" and
    answer 404, which reads as an empty list.

Ordering in `main.py` is the fix, the reserved-slug list is a second guard so no author can claim
one of those names, and this file is the third, because ordering is invisible at review time and
an `include_router` line is easy to move while tidying.
"""

import os

import pytest
from starlette.routing import compile_path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
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

    important: matched on the SERVED path from `_walk_route_paths`, not on `route.matches`.
    From FastAPI 0.141 `include_router` stores a nested router whose routes carry only their
    own prefix, so `route.matches` on `/api/v1/extensions/self` found nothing on the deployed
    image while passing in a venv on 0.128. requirements.txt allows both shapes. Declaration
    order is kept by the walk, which is what lets the literal route win over the slug.
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
    ],
)
def test_a_literal_route_wins_over_the_slug_route(app, method, path, expected) -> None:
    assert _resolve(app, method, path) == expected


def test_every_literal_first_segment_is_a_reserved_slug(app) -> None:
    """The second guard, checked against the routes rather than against a memory of them.

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

    # important: the positive twin. On FastAPI 0.141 the old walk reported no path under
    # `/api/v1/extensions/` at all, and this guard passed on an empty set while guarding nothing.
    assert {"self", "vocabulary", "installations"} <= literals, (
        f"the walk found {sorted(literals)}: it is not seeing the served extension routes"
    )
    unreserved = sorted(literals - RESERVED_SLUGS)
    assert not unreserved, (
        f"these route segments can be claimed as an add-on slug: {unreserved}. "
        "Add them to RESERVED_SLUGS, or an author shadows a platform route."
    )
