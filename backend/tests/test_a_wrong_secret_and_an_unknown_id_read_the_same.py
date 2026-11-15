"""Header authentication answers a wrong secret exactly as it answers an unknown key id.

The two answered "Invalid API key secret" and "Invalid API key", so anyone could learn which key
ids exist by trying any secret. Both now read "Invalid API key", as the comment on the 404 mapping
and the integrator document already said.
"""

import pytest
from fastapi import HTTPException

from app.routers import integrations


def test_a_wrong_secret_reads_as_an_invalid_key(monkeypatch) -> None:
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, key_id, use_cache=True: ({"id": key_id}, "h"))
    monkeypatch.setattr(integrations, "verify_api_key_secret", lambda secret, stored: False)
    with pytest.raises(HTTPException) as refused:
        integrations._validate_api_key_blocking("oak_abc123", "wrong")
    assert refused.value.status_code == 401 and refused.value.detail == "Invalid API key"
