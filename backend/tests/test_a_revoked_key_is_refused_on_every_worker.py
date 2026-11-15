"""A key validated after a local cache miss is read from Vault itself, not the shared mirror.

The metadata path is mirrored in Redis for ten minutes, and a read that started before a revoke
can put the old copy back after the revoke cleared it (core/vault_client.py). The validation miss
path read through that mirror, so a worker that missed its local cache could accept a revoked key
until the mirror expired, long after the cross-worker tombstone (about 43 s) was gone. It now reads
Vault directly; the per-process cache still absorbs the load.
"""

import pytest
from fastapi import HTTPException

from app.routers import integrations


def test_the_validation_miss_path_skips_the_shared_mirror(monkeypatch) -> None:
    reads = []

    def read(_vault, key_id, use_cache=True):
        reads.append(use_cache)
        return {"id": key_id, "is_revoked": True}, "stored-hash"

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", read)
    monkeypatch.setattr(integrations, "verify_api_key_secret", lambda secret, stored: False)

    with pytest.raises(HTTPException):
        integrations._validate_api_key_blocking("oak_abc123", "secret")
    assert reads == [False]
