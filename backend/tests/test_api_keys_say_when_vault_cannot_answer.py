"""While Vault is sealed or unreachable, API keys answer 503, never "invalid" and never "none".

An integration reads a 401 as a revoked credential and an operator reads an empty key list as
every key gone, so an outage answers 503 `api_key_store_unavailable`, as /clusters answers
vault_unavailable. A 404 from Vault still means the key is not there.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import requests
from fastapi import HTTPException

from app.core.vault_client import VaultClientError
from app.routers import integrations

UNAVAILABLE = "api_key_store_unavailable"


class _Vault:
    vault_addr = "http://vault.test"
    headers: Dict[str, str] = {}

    def __init__(self, *, read_status: int | None = 200, list_status: int = 200, put_error: Exception | None = None):
        self.read_status = read_status
        self.list_status = list_status
        self.put_error = put_error
        self.deleted: List[str] = []
        outer = self

        class _Session:
            def request(self, method: str, url: str, **_kw: Any) -> Any:
                class _Response:
                    status_code = outer.list_status

                    def raise_for_status(self) -> None:
                        if self.status_code >= 400:
                            error = requests.HTTPError(f"HTTP {self.status_code}")
                            error.response = self  # type: ignore[attr-defined]
                            raise error

                    def json(self) -> Dict[str, Any]:
                        return {"data": {"keys": ["oak_one/"]}}

                return _Response()

        self.session = _Session()

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if self.read_status is None:
            # As vault_client.vault_read raises it: inside the transport error's except block.
            try:
                raise requests.ConnectionError("connection refused")
            except requests.ConnectionError:
                raise VaultClientError("Vault read failed", status_code=None)
        if self.read_status != 200:
            raise VaultClientError("Vault read failed", status_code=self.read_status)
        return {"id": "oak_one", "hash": "h"}

    def vault_kv_put(self, path: str, data: Any) -> None:
        if self.put_error:
            raise self.put_error

    def vault_delete_metadata(self, path: str) -> bool:
        self.deleted.append(path)
        return True


def _code(caught: pytest.ExceptionInfo) -> str:
    detail = caught.value.detail
    return detail.get("error") if isinstance(detail, dict) else str(detail)


@pytest.mark.parametrize("status", [503, 500, None], ids=["sealed", "server-error", "unreached"])
def test_a_key_read_while_vault_cannot_answer_is_503(status) -> None:
    with pytest.raises(HTTPException) as caught:
        integrations.get_api_key_from_vault(_Vault(read_status=status), "oak_one")
    assert caught.value.status_code == 503
    assert _code(caught) == UNAVAILABLE
    assert caught.value.headers.get("Retry-After")


def test_a_key_vault_does_not_hold_is_still_not_found() -> None:
    with pytest.raises(HTTPException) as caught:
        integrations.get_api_key_from_vault(_Vault(read_status=404), "oak_gone")
    assert caught.value.status_code == 404


def test_the_key_list_is_503_when_vault_cannot_list() -> None:
    with pytest.raises(HTTPException) as caught:
        integrations.list_api_keys_from_vault(_Vault(list_status=503))
    assert caught.value.status_code == 503
    assert _code(caught) == UNAVAILABLE


def test_the_key_list_is_503_when_a_key_read_fails_part_way() -> None:
    with pytest.raises(HTTPException) as caught:
        integrations.list_api_keys_from_vault(_Vault(read_status=503))
    assert caught.value.status_code == 503


def test_an_empty_key_store_is_still_an_empty_list() -> None:
    assert integrations.list_api_keys_from_vault(_Vault(list_status=404)) == []


def test_a_mint_vault_cannot_store_is_503_not_500() -> None:
    vault = _Vault(put_error=VaultClientError("Vault KV put failed", status_code=503))
    with pytest.raises(HTTPException) as caught:
        integrations.save_api_key_to_vault(vault, "oak_new", {"id": "oak_new"}, "hash")
    assert caught.value.status_code == 503
    assert _code(caught) == UNAVAILABLE


def test_the_answer_keeps_its_code_through_the_sanitizer() -> None:
    from app.core.error_sanitizer import public_server_detail

    shown = public_server_detail(503, {"error": UNAVAILABLE, "message": "API keys cannot be read."}, "ref")
    assert shown == {"error": UNAVAILABLE, "message": "API keys cannot be read."}


def test_a_malformed_key_id_is_still_not_found() -> None:
    """The client refuses a dot or empty path segment before any request: not an outage.

    If `..` as a key id reached the 503, anyone could fake the "Vault is down" answer, and an
    integration with a bad id would retry forever.
    """

    class _Refusing(_Vault):
        def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
            raise VaultClientError(f"Refusing Vault path with a traversal or empty segment: {path!r}")

    with pytest.raises(HTTPException) as caught:
        integrations.get_api_key_from_vault(_Refusing(), "..")
    assert caught.value.status_code == 404


def test_one_unreadable_key_document_does_not_hide_the_rest() -> None:
    """A 500 on one key's document while Vault answers the LIST skips only that document."""

    class _OneBroken(_Vault):
        def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
            raise VaultClientError("Vault read failed", status_code=500)

    assert integrations.list_api_keys_from_vault(_OneBroken()) == []


def test_the_public_validate_endpoint_says_unavailable_not_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: _Vault(read_status=503))
    request = integrations.APIKeyValidateRequest(api_key_id="oak_one", api_key_secret="oas_x")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(integrations.validate_api_key_endpoint(request))
    assert caught.value.status_code == 503
