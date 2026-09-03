"""The unauthenticated surface may name the version. It may not name the commit or the build date.

f270eb0 took the build commit and date off `/` for a reason that has not changed: `/` and `/health`
need no credential, `/health` is excluded from access logging, and together they name the exact
source an anonymous caller is talking to while leaving no trace that anyone asked. The version
stayed, because clients and the footer read it.

Publishing the version on `/health` as well discloses nothing new, since `/` already hands it to the
same callers. Publishing anything more would quietly undo the remediation, and nothing in the tree
would have said so. This is that thing.

Hermetic: `create_app()` builds the app in process, and `TestClient` outside a `with` block runs no
lifespan, so no database, no Vault and no cluster are touched.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import build_identity  # noqa: E402
from app.main import create_app  # noqa: E402

WITHHELD = ("commit", "built_at", "build_commit", "build_date")


def _client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_reports_the_version():
    r = _client().get("/health")
    assert r.status_code == 200, r.text
    assert "version" in r.json(), r.text


def test_health_withholds_the_commit_and_the_build_date():
    body = _client().get("/health").json()
    for field in WITHHELD:
        assert field not in body, f"{field} must not reach an unauthenticated caller: {body}"


def test_the_root_endpoint_still_withholds_them_too():
    """The endpoint the original hardening was about. Both must stay closed, not just the new one."""
    body = _client().get("/").json()
    assert "version" in body, body
    for field in WITHHELD:
        assert field not in body, f"{field} must not reach an unauthenticated caller: {body}"


def test_health_keeps_the_identifier_monitoring_keys_on():
    """`service` is registered as the Authentik OAuth2 audience. A new field must not disturb it."""
    body = _client().get("/health").json()
    assert body["service"] == "orcastra-dashboard-api", body
    assert body["status"] == "healthy", body


def test_the_public_shape_is_the_version_and_nothing_else():
    """Asserted on the helper as well, so a future caller cannot widen it by reaching past `public()`."""
    assert set(build_identity.public()) == {"version"}
