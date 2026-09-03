"""A caller holding a key learns which build answered it. A caller holding nothing does not.

An integrator could not previously find out which build of this server it was talking to. The
version exists on `/` and the full identity on `/api/v1/system/version`, but that router requires a
tenant session, and an integration authenticates with an API key, so the one surface it can reach
said nothing. The practical cost is support: "which version is this partner on" could only be
answered by asking them.

It rides the two responses a key already receives rather than a new endpoint, so it costs no extra
request and an older build simply omits the field. `/validate` verifies the presented secret before
it reaches the success branch and answers every refusal with the same wording to avoid becoming a
key-id oracle, so the identity is credential-gated even though the route takes no header auth.

important: self-reported. It says which build this deployment believes it is running. It is not
evidence the deployment is genuine, and no caller should treat it as such.

Hermetic: the handler is awaited directly with a fake validated key, so no Vault, no database and no
network are touched.
"""
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import build_identity  # noqa: E402
from app.routers.integrations import APIKeyValidateResponse, ServerIdentity, whoami  # noqa: E402


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


def test_whoami_names_the_build_that_answered():
    server = _whoami().server
    assert server is not None, "a validated key must learn which build answered it"
    assert server.version == build_identity.version()
    assert server.commit == build_identity.commit()
    assert server.built_at == build_identity.built_at()


def test_whoami_still_describes_the_key_itself():
    """The field is additive. What the endpoint already answered must be untouched."""
    response = _whoami()
    assert response.valid is True
    assert response.app_name == "orcahub"
    assert response.capabilities == ["inventory.read"]


def test_a_refusal_names_no_build():
    """Every `/validate` failure branch constructs the model without a server, so it stays null.

    This is what keeps the identity behind the credential: a caller who cannot authenticate gets the
    same uniform refusal it always got, carrying nothing about the deployment.
    """
    for refusal in (
        APIKeyValidateResponse(valid=False, error="Invalid API key or secret"),
        APIKeyValidateResponse(valid=False, error="API key revoked"),
        APIKeyValidateResponse(valid=False, error="API key not active"),
        APIKeyValidateResponse(valid=False, error="API key expired"),
        APIKeyValidateResponse(valid=False, error="Rate limit exceeded for this API key"),
        APIKeyValidateResponse(valid=False, error="Validation failed"),
    ):
        assert refusal.server is None, refusal


def test_the_block_carries_all_three_fields_and_no_more():
    """A reader on the other side keys on these names. Adding to them is a contract change."""
    assert set(ServerIdentity().model_dump()) == {"version", "commit", "built_at"}


def test_an_unstamped_build_is_null_rather_than_absent():
    """The field is always present on an authenticated response, so a reader never has to guess
    whether a missing key means an old build or an unstamped one."""
    dumped = APIKeyValidateResponse(valid=True, server=ServerIdentity()).model_dump()
    assert "server" in dumped
    assert dumped["server"] == {"version": None, "commit": None, "built_at": None}
