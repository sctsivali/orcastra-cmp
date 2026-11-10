"""The two policy endpoints authenticate through the shared API key dependency.

`generate_api_key_pair` stores HMAC-SHA256(SECRET_KEY, secret) and `verify_api_key_secret`
recomputes it the same way. A handler that compares a plain SHA-256 of the presented secret with
that stored hash can never match, so it answers 401 to every caller, and an inline check also
skips the expiry check and the cross-worker revocation tombstone that `validate_api_key` performs.

`POST /integrations/external/sync-policies` and `GET /integrations/external/policies` take their
key from that dependency, and no handler hashes a presented secret itself.
"""

import ast
import inspect
import os
from pathlib import Path

import pytest

import app.routers.integrations as integrations_module
from app.routers.integrations import (
    generate_api_key_pair,
    get_policies_for_external,
    sync_policies_from_external,
    verify_api_key_secret,
)

ROUTER_SOURCE = Path(integrations_module.__file__).read_text()


@pytest.fixture(autouse=True)
def _secret_key():
    """generate_api_key_pair reads SECRET_KEY from the environment and 500s without one."""
    previous = os.environ.get("SECRET_KEY")
    os.environ["SECRET_KEY"] = "test-secret-key-for-hashing"
    yield
    if previous is None:
        os.environ.pop("SECRET_KEY", None)
    else:
        os.environ["SECRET_KEY"] = previous


def test_a_plain_sha256_can_never_match_an_issued_key() -> None:
    """A plain SHA-256 of the secret never equals the stored keyed hash."""
    import hashlib

    _key_id, key_secret, stored_hash = generate_api_key_pair()

    assert verify_api_key_secret(key_secret, stored_hash), "the real verifier must accept it"

    plain = hashlib.sha256(key_secret.encode()).hexdigest()
    assert plain != stored_hash, (
        "if these were equal the old endpoints would have worked and there is nothing to fix"
    )


@pytest.mark.parametrize(
    "handler", [sync_policies_from_external, get_policies_for_external]
)
def test_the_handler_takes_its_key_from_the_shared_dependency(handler) -> None:
    """The key comes from the shared dependency, never from headers the handler parses itself."""
    parameters = inspect.signature(handler).parameters
    assert "api_key" in parameters, f"{handler.__name__} does not take a validated key"

    default = parameters["api_key"].default
    rendered = repr(default.dependency) if hasattr(default, "dependency") else repr(default)
    assert "require_tenant_provision" in rendered, (
        f"{handler.__name__} must authenticate through require_capability('tenant.provision'), "
        f"got {rendered}"
    )

    assert "request" not in parameters, (
        f"{handler.__name__} still takes a raw Request, which is how it read the headers itself"
    )


def test_no_handler_hashes_a_presented_secret_by_hand() -> None:
    """verify_api_key_secret is the only place a presented secret may be hashed.

    A second copy can drift onto a scheme the issuer never used.
    """
    tree = ast.parse(ROUTER_SOURCE)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in ("generate_api_key_pair", "verify_api_key_secret", "_api_key_digest"):
            continue
        source = ast.unparse(node)
        if "hashlib.sha256(api_key_secret" in source or "hashlib.sha256(key_secret" in source:
            offenders.append(node.name)
    assert not offenders, (
        "these functions hash a presented secret themselves instead of calling "
        f"verify_api_key_secret: {offenders}"
    )
