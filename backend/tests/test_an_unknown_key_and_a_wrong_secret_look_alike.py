"""
An unknown API key id and a wrong secret get the same answer on every external endpoint.

The header dependency said "Invalid API key" for an unknown id and "Invalid API key secret" for a
wrong secret, /validate said "API key not found" and "Invalid secret", and the two policy
endpoints did the same. The difference told a caller which key ids exist. Revoked, inactive and
expired keep their own texts, because reaching those already needs the correct secret.
"""

import hashlib
import hmac
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.cache import get_cache_manager
from app.routers import integrations
from app.routers.integrations import (
    INVALID_API_KEY_MESSAGE,
    APIKeyValidateRequest,
    SyncPoliciesRequest,
    get_policies_for_external,
    sync_policies_from_external,
    validate_api_key,
    validate_api_key_endpoint,
)

SECRET_KEY = "unit-test-secret-key"
KEY_ID = "oak_test"
KEY_SECRET = "oas_right"
PREFIX = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys"


class FakeVault:
    def __init__(self, metadata: Dict[str, Any]) -> None:
        digest = hmac.new(SECRET_KEY.encode(), KEY_SECRET.encode(), hashlib.sha256).hexdigest()
        self.store: Dict[str, Dict[str, Any]] = {
            f"{PREFIX}/{KEY_ID}/metadata": metadata,
            f"{PREFIX}/{KEY_ID}/secret": {"hash": digest, "raw": KEY_SECRET},
        }
        self.writes: List[Tuple[str, Dict[str, Any]]] = []

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if path not in self.store:
            raise RuntimeError(f"Vault read failed: 404 for {path}")
        return dict(self.store[path])

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        self.writes.append((path, data))
        return {}


def _metadata(**overrides: Any) -> Dict[str, Any]:
    base = {
        "id": KEY_ID,
        "app_name": "qa",
        "app_type": "custom",
        "is_active": True,
        "is_revoked": False,
        "expires_at": None,
        "cluster_permissions": [],
    }
    base.update(overrides)
    return base


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeVault]:
    fake = FakeVault(_metadata())
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: fake)

    def _reset() -> None:
        with integrations._key_cache_lock:
            integrations._key_cache.clear()
            integrations._last_used_written.clear()
        try:
            get_cache_manager().delete(integrations._REVOKED_NS, KEY_ID)
        except Exception:
            pass

    _reset()
    yield fake
    _reset()


async def _header_detail(key_id: str, secret: str) -> Tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        await validate_api_key(key_id, secret)
    return caught.value.status_code, caught.value.detail


async def test_the_header_dependency_gives_one_answer(vault: FakeVault) -> None:
    unknown = await _header_detail("oak_missing", KEY_SECRET)
    wrong = await _header_detail(KEY_ID, "oas_wrong")
    assert unknown == wrong == (401, INVALID_API_KEY_MESSAGE)


async def test_the_header_dependency_still_accepts_the_right_secret(vault: FakeVault) -> None:
    metadata = await validate_api_key(KEY_ID, KEY_SECRET)
    assert metadata["id"] == KEY_ID


async def test_validate_gives_one_answer(vault: FakeVault) -> None:
    unknown = await validate_api_key_endpoint(
        APIKeyValidateRequest(api_key_id="oak_missing", api_key_secret=KEY_SECRET)
    )
    wrong = await validate_api_key_endpoint(
        APIKeyValidateRequest(api_key_id=KEY_ID, api_key_secret="oas_wrong")
    )
    assert unknown.valid is False and wrong.valid is False
    assert unknown.error == wrong.error == INVALID_API_KEY_MESSAGE


async def test_validate_keeps_the_revoked_text_behind_the_right_secret(
    vault: FakeVault,
) -> None:
    vault.store[f"{PREFIX}/{KEY_ID}/metadata"] = _metadata(is_revoked=True)
    revoked = await validate_api_key_endpoint(
        APIKeyValidateRequest(api_key_id=KEY_ID, api_key_secret=KEY_SECRET)
    )
    assert revoked.error == "API key revoked"


def _request(key_id: str, secret: str) -> Request:
    return Request({
        "type": "http",
        "headers": [(b"x-api-key-id", key_id.encode()), (b"x-api-key-secret", secret.encode())],
    })


async def _sync_policies_detail(key_id: str, secret: str) -> Tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        await sync_policies_from_external(
            body=SyncPoliciesRequest(policies=[]), request=_request(key_id, secret), db=None
        )
    return caught.value.status_code, caught.value.detail


async def _get_policies_detail(key_id: str, secret: str) -> Tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        await get_policies_for_external(
            org_external_id=None, request=_request(key_id, secret), db=None
        )
    return caught.value.status_code, caught.value.detail


@pytest.mark.parametrize("call", [_sync_policies_detail, _get_policies_detail], ids=["sync", "get"])
async def test_the_policy_endpoints_give_one_answer(vault: FakeVault, call: Any) -> None:
    unknown = await call("oak_missing", KEY_SECRET)
    wrong = await call(KEY_ID, "oas_wrong")
    assert unknown == wrong == (401, INVALID_API_KEY_MESSAGE)
