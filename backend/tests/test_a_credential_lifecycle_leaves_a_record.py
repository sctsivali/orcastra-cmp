"""Issuing and revoking an integration credential left nothing an operator could look at.

Two separate gaps, one subject.

The first is the audit trail. `create_api_key` wrote no record at all. `revoke_api_key` and
`delete_api_key` wrote one `logger.info` apiece, which goes to the application log rather than the
audit index and is subject to `LogDeduplicationFilter`. So "who minted the credential that is
reaching this tenant's cluster, and when" had no answer, on a surface whose whole job is handing
third parties access to customer infrastructure. `_audit_key_action` was already there for the
key-authenticated side; nothing covered the operator-authenticated side that creates the key in
the first place.

The second is the recoverable copy of the secret. `save_api_key_to_vault` stores an HMAC of the
secret, and `verify_api_key_secret` compares against it, so the design is hash-only. But
`_validate_api_key_blocking` also wrote the PLAINTEXT secret back into Vault on every key's first
successful authentication. The only reader of that field is `orcahub_sync._get_orcahub_credentials`,
which returns None unless the key carries `app_url`. Every key without a callback base therefore
accumulated a plaintext credential in Vault that nothing would ever read: pure liability, and it
would have made "extension credentials are hash-only" a false claim the moment an extension made
its first request.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.routers import integrations
from app.routers.integrations import (
    _validate_api_key_blocking,
    generate_api_key_pair,
)
from app.services.activity_logger import (
    ACTION_CATEGORIES,
    HIGH_SEVERITY_ACTIONS,
    EventCategory,
)

LIFECYCLE_ACTIONS = (
    "integration.api_key.create",
    "integration.api_key.revoke",
    "integration.api_key.delete",
)


# ── the taxonomy ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", LIFECYCLE_ACTIONS)
def test_the_action_is_registered(action: str) -> None:
    """An unregistered action still logs, but silently lands in the wrong category.

    `ACTION_CATEGORIES.get(action, EventCategory.LXD_INFRA)` means a typo files a credential
    grant under infrastructure and nobody notices.
    """
    assert ACTION_CATEGORIES.get(action) is EventCategory.ADMIN


@pytest.mark.parametrize("action", LIFECYCLE_ACTIONS)
def test_the_action_is_high_severity(action: str) -> None:
    """Minting and revoking third-party access to a customer's clusters are both sharp."""
    assert action in HIGH_SEVERITY_ACTIONS


# ── the record itself ──────────────────────────────────────────────────────────────────────────

class _RecordingLogger:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "evt_test"


@pytest.fixture
def recorded(monkeypatch) -> _RecordingLogger:
    logger = _RecordingLogger()
    monkeypatch.setattr(integrations, "activity_logger", logger)
    return logger


def test_the_record_names_the_key_the_grant_and_never_the_secret(recorded) -> None:
    integrations._audit_credential_lifecycle(
        action="integration.api_key.create",
        key_id="oak_abc123",
        metadata={
            "app_name": "QA Extension",
            "app_type": "automation",
            "capabilities": ["inventory.read"],
            "cluster_permissions": [{"cluster_id": "nuc2", "permissions": ["read"]}],
            "expires_at": "2027-01-01T00:00:00+00:00",
            # A secret must never reach the audit even if a caller hands the whole blob over.
            "api_key_secret": "oas_do_not_log_me",
        },
    )

    assert len(recorded.calls) == 1
    call = recorded.calls[0]
    assert call["action"] == "integration.api_key.create"
    assert call["target_type"] == "api_key"
    assert call["target_id"] == "oak_abc123"

    details = call["details"]
    assert details["api_key_id"] == "oak_abc123"
    assert details["app_name"] == "QA Extension"
    assert details["capabilities"] == ["inventory.read"]
    assert details["cluster_ids"] == ["nuc2"]
    # The helper reduces the metadata to the fields that describe the grant rather than
    # forwarding it whole, so a secret sitting in the blob cannot ride along.
    assert "oas_do_not_log_me" not in repr(details)


def test_a_failed_audit_write_does_not_fail_the_request(monkeypatch) -> None:
    """A broken audit sink must not stop an operator revoking a live credential."""
    class _Exploding:
        def log_activity(self, **_: Any) -> str:
            raise RuntimeError("audit sink is down")

    monkeypatch.setattr(integrations, "activity_logger", _Exploding())
    integrations._audit_credential_lifecycle(
        action="integration.api_key.revoke", key_id="oak_abc123", metadata={}
    )


# ── the plaintext backfill ─────────────────────────────────────────────────────────────────────

class _FakeVault:
    """Records what was written, so the test asserts state rather than absence of an exception."""

    def __init__(self, metadata: Dict[str, Any], secret_hash: str) -> None:
        self.metadata = metadata
        self.secret_payload: Dict[str, Any] = {"hash": secret_hash}
        self.writes: List[Tuple[str, Dict[str, Any]]] = []

    def vault_read(self, path: str) -> Dict[str, Any]:
        if path.endswith("/metadata"):
            return self.metadata
        return self.secret_payload

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        self.writes.append((path, data))
        if path.endswith("/secret"):
            self.secret_payload = data


def _validate_with(monkeypatch, app_url: Optional[str]) -> _FakeVault:
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    key_id, key_secret, secret_hash = generate_api_key_pair()

    metadata: Dict[str, Any] = {"id": key_id, "app_name": "test", "capabilities": []}
    if app_url is not None:
        metadata["app_url"] = app_url

    vault = _FakeVault(metadata, secret_hash)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)

    _validate_api_key_blocking(key_id, key_secret)
    return vault


def test_a_key_without_a_callback_stays_hash_only(monkeypatch) -> None:
    """The claim an issued extension credential makes, asserted against Vault's contents."""
    vault = _validate_with(monkeypatch, app_url=None)

    assert vault.writes == [], "authenticating wrote something back to Vault"
    assert "raw" not in vault.secret_payload
    assert vault.secret_payload.get("hash")


def test_a_key_with_a_callback_still_gets_the_copy_its_callbacks_need(monkeypatch) -> None:
    """The half that has to keep working.

    OrcaHub's outbound member sync re-presents this secret. Removing the backfill outright would
    break a live integration, which is why the fix is a guard and not a deletion.
    """
    vault = _validate_with(monkeypatch, app_url="https://partner.example.com")

    assert vault.writes, "a callback key did not get the copy its callbacks read"
    assert vault.secret_payload.get("raw")


def test_the_backfill_does_not_repeat_once_it_is_there(monkeypatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    key_id, key_secret, secret_hash = generate_api_key_pair()
    vault = _FakeVault(
        {"id": key_id, "app_url": "https://partner.example.com"}, secret_hash
    )
    vault.secret_payload = {"hash": secret_hash, "raw": key_secret}
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)

    _validate_api_key_blocking(key_id, key_secret)
    assert vault.writes == []
