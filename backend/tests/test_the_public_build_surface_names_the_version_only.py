"""The unauthenticated surface may name the version. It may not name the commit or the build date.

`/` and `/health` need no credential and `/health` is excluded from access logging, so a commit and
build date there would name the exact source an anonymous caller is talking to while leaving no
trace that anyone asked. The version stays, because clients and the footer read it; publishing it
on both endpoints discloses nothing new.

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

# What `/health` is allowed to answer, exactly. An allow-list fails closed on a renamed or new field,
# and the failure names the new key. Monitoring keys on `status` and `service`, so widening this is
# a contract change.
HEALTH_KEYS = {"status", "service", "version", "temp_storage", "vault", "identity_provider"}


def _every_key(node) -> set:
    """Every key anywhere in the body. A withheld value hidden one level down is still published."""
    found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= _every_key(value)
    elif isinstance(node, list):
        for item in node:
            found |= _every_key(item)
    return found


def _client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_reports_the_version() -> None:
    r = _client().get("/health")
    assert r.status_code == 200, r.text
    assert "version" in r.json(), r.text


def test_health_withholds_the_commit_and_the_build_date() -> None:
    body = _client().get("/health").json()
    for field in WITHHELD:
        assert field not in _every_key(body), (
            f"{field} must not reach an unauthenticated caller: {body}"
        )


def test_health_answers_these_keys_and_no_others() -> None:
    """Set equality, so a new top-level field has to be added here explicitly."""
    assert set(_client().get("/health").json()) == HEALTH_KEYS


def test_the_root_endpoint_still_withholds_them_too() -> None:
    """`/` names the version and withholds the commit and the build date, like `/health`."""
    body = _client().get("/").json()
    assert "version" in body, body
    for field in WITHHELD:
        assert field not in _every_key(body), (
            f"{field} must not reach an unauthenticated caller: {body}"
        )


def test_both_public_surfaces_answer_the_same_version() -> None:
    """They read the same helper. If one is rewired to the setting directly, an unstamped build
    answers "" on one and null on the other, and a client testing for null believes the wrong one."""
    client = _client()
    assert client.get("/").json()["version"] == client.get("/health").json()["version"]


def test_health_keeps_the_identifier_monitoring_keys_on() -> None:
    """`service` is registered as the Authentik OAuth2 audience. A new field must not disturb it."""
    body = _client().get("/health").json()
    assert body["service"] == "orcastra-dashboard-api", body
    assert body["status"] == "healthy", body


def test_the_public_shape_is_the_version_and_nothing_else() -> None:
    """Asserted on the helper as well, so a future caller cannot widen it by reaching past `public()`."""
    assert set(build_identity.public()) == {"version"}


def test_the_identity_provider_field_says_only_what_it_should() -> None:
    """A class name and counters, never the issuer, the JWKS URL or a key."""
    body = _client().get("/health").json()["identity_provider"]
    assert set(body) in (
        {"enabled"},
        {"enabled", "keys_held", "key_count", "age_seconds", "serving_stale", "max_stale_seconds", "last_error"},
    ), body
    assert "http" not in str(body).lower()
