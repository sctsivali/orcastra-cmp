"""A caller holding a key learns which build answered it. A caller holding nothing does not.

`/api/v1/system/version` requires a tenant session and an integration holds only an API key, so
the build identity rides the two responses a key already receives (`whoami` and `/validate`); an
older build omits the field. `/validate` verifies the presented secret before its success branch
and answers every refusal with the same wording, so it is no key-id oracle and the identity stays
credential-gated although the route takes no header auth.

important: the identity is self-reported. It names the build this deployment believes it runs and
is no evidence that the deployment is genuine.

The handlers are awaited directly; `/validate` runs for real with only Vault replaced.
"""
import asyncio
import hashlib
import hmac
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import build_identity  # noqa: E402
from app.routers import integrations  # noqa: E402
from app.routers.integrations import (  # noqa: E402
    APIKeyValidateRequest,
    APIKeyValidateResponse,
    ServerIdentity,
    validate_api_key_endpoint,
    whoami,
)


def _key() -> Dict[str, Any]:
    """What `validate_api_key` hands a handler once the secret has verified."""
    return {
        "id": "oak_test",
        "app_name": "orcahub",
        "app_type": "orcahub",
        "capabilities": ["inventory.read"],
        "cluster_permissions": [],
    }


def _whoami() -> APIKeyValidateResponse:
    return asyncio.run(whoami(api_key=_key()))


def test_whoami_names_the_build_that_answered() -> None:
    server = _whoami().server
    assert server is not None, "a validated key must learn which build answered it"
    assert server.version == build_identity.version()
    assert server.commit == build_identity.commit()
    assert server.built_at == build_identity.built_at()


def test_whoami_still_describes_the_key_itself() -> None:
    """The field is additive; the rest of the answer is unchanged."""
    response = _whoami()
    assert response.valid is True
    assert response.app_name == "orcahub"
    assert response.capabilities == ["inventory.read"]


def _validate(
    monkeypatch: pytest.MonkeyPatch, metadata: Dict[str, Any], *, secret: str
) -> APIKeyValidateResponse:
    """Drive the real `/validate` handler with a fake Vault behind it.

    Only Vault is replaced, so the secret comparison, the revoked/inactive/expiry branches and the
    response construction are the real ones, and a server block on a refusal branch fails here.
    """
    # The hashing key is read from the environment on every call and a missing one is a 500, so it
    # is set before the hash is computed as well as before the handler verifies against it.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-hmac-only")
    stored_hash = hmac.new(
        integrations._get_hmac_key(), b"oas_the_real_secret", hashlib.sha256
    ).hexdigest()

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(
        integrations,
        "get_api_key_from_vault",
        lambda _client, _key_id: (metadata, stored_hash),
    )
    return asyncio.run(
        validate_api_key_endpoint(
            APIKeyValidateRequest(api_key_id="oak_test", api_key_secret=secret)
        )
    )


ACTIVE = {"is_revoked": False, "is_active": True, "expires_at": None, "app_name": "orcahub"}


@pytest.mark.parametrize(
    "metadata, secret, expected_error",
    [
        (ACTIVE, "oas_the_wrong_secret", "Invalid API key or secret"),
        ({**ACTIVE, "is_revoked": True}, "oas_the_real_secret", "API key revoked"),
        ({**ACTIVE, "is_active": False}, "oas_the_real_secret", "API key not active"),
        (
            {**ACTIVE, "expires_at": "2020-01-01T00:00:00+00:00"},
            "oas_the_real_secret",
            "API key expired",
        ),
    ],
)
def test_a_refusal_names_no_build(
    monkeypatch: pytest.MonkeyPatch,
    metadata: Dict[str, Any],
    secret: str,
    expected_error: str,
) -> None:
    """A caller who cannot authenticate learns nothing about the deployment.

    It drives the handler: a response model built by hand carries the field's default and would
    pass whatever the handler did.
    """
    response = _validate(monkeypatch, metadata, secret=secret)

    assert response.valid is False
    assert response.error == expected_error
    assert response.server is None, "a refusal must carry no build identity"
    assert "server" not in response.model_dump(exclude_none=True)


def test_the_success_branch_is_the_one_that_names_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secret is the gate: a verified one reaches the branch that names the build."""
    response = _validate(monkeypatch, ACTIVE, secret="oas_the_real_secret")

    assert response.valid is True
    assert response.server is not None
    assert response.server.version == build_identity.version()
    assert response.server.commit == build_identity.commit()


def test_the_block_carries_all_three_fields_and_no_more() -> None:
    """A reader on the other side keys on these names. Adding to them is a contract change."""
    assert set(ServerIdentity().model_dump()) == {"version", "commit", "built_at"}


def test_an_unstamped_build_is_null_rather_than_absent() -> None:
    """The field is always present on an authenticated response, so a reader never has to guess
    whether a missing key means an old build or an unstamped one."""
    dumped = APIKeyValidateResponse(valid=True, server=ServerIdentity()).model_dump()
    assert "server" in dumped
    assert dumped["server"] == {"version": None, "commit": None, "built_at": None}
