"""API key creation, permission changes, revocation and deletion leave an audit trail.

integrations.py never called the activity logger, so a key minted, widened, revoked or deleted
left nothing in orcastra-audit-*. Each lifecycle step now records one event built from an
allowlist: the secret, its hash and app_url never reach the log, and the key id travels as the
target id because the audit sanitiser blanks any detail whose name contains api_key.
"""

import json
import logging
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import integrations
from app.routers.integrations import APIKeyCreateRequest, ClusterPermission
from app.services.activity_logger import ActivityLogger

PARTNER = AuthenticatedUser(user_id="partner", email="partner@example.test", groups=["role_partner"])

STORED: Dict[str, Any] = {
    "id": "oak_test",
    "name": "k",
    "app_name": "app",
    "app_type": "orcahub",
    "created_by": "partner@example.test",
    "created_at": "2026-01-01T00:00:00",
    "expires_at": None,
    "app_url": "https://example.test/APP-URL-MARKER",
    "raw": "RAW-SECRET-MARKER",
    "hash": "HASH-MARKER",
    "cluster_permissions": [
        {"cluster_id": "cluster-a", "projects": ["*"], "permissions": ["read"], "junk": "JUNK-MARKER"}
    ],
}
MARKERS = ("APP-URL-MARKER", "RAW-SECRET-MARKER", "HASH-MARKER", "JUNK-MARKER")


async def _returns_copy_of_stored(_key_id: str, _user: Any, _db: Any) -> Dict[str, Any]:
    return json.loads(json.dumps(STORED))


class Spy:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> str:
        self.events.append(kwargs)
        return "evt"


class FakeVault:
    def __init__(self) -> None:
        self.puts: List[tuple] = []

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        self.puts.append((path, dict(data)))


class _Cache:
    def delete_pattern(self, *_a: Any, **_k: Any) -> None:
        return None

    def delete(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def env(monkeypatch):
    spy = Spy()
    vault = FakeVault()
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    monkeypatch.setattr(integrations, "activity_logger", spy)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    monkeypatch.setattr(integrations, "_authorize_api_key", _returns_copy_of_stored)
    monkeypatch.setattr(
        integrations, "get_api_key_from_vault", lambda _v, _k: (json.loads(json.dumps(STORED)), "HASH-MARKER")
    )
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "delete_api_key_from_vault", lambda _v, _k: None)

    async def _no_reconcile(*_a: Any, **_k: Any) -> int:
        return 0

    async def _no_bound(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _no_reconcile)
    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", _no_bound)
    token = org_scope._allowed_clusters_var.set(None)
    yield {"spy": spy, "vault": vault}
    org_scope._allowed_clusters_var.reset(token)


def _request() -> APIKeyCreateRequest:
    return APIKeyCreateRequest(
        name="k", app_name="app",
        cluster_permissions=[ClusterPermission(cluster_id="cluster-a", permissions=["read"], projects=["*"])],
    )


def _clean(event: Dict[str, Any], *extra: str) -> None:
    text = json.dumps(event, default=str)
    for marker in MARKERS + extra:
        assert marker not in text, f"{marker} reached the audit event"


async def test_create_records_one_event_without_the_secret(env):
    response = await integrations.create_api_key(request=_request(), _=None, _rbac=PARTNER, db=None)
    events = env["spy"].events
    assert [e["action"] for e in events] == ["integration.api_key.create"]
    event = events[0]
    assert event["target_id"] == response.api_key_id
    assert event["target_type"] == "integration_api_key"
    assert event["details"]["actor_email"] == "partner@example.test"
    assert event["details"]["clusters"] == ["cluster-a"]
    secret_hash = env["vault"].puts[1][1]["hash"]
    _clean(event, response.api_key_secret, secret_hash)


async def test_a_denied_create_is_recorded(env):
    token = org_scope._allowed_clusters_var.set({"cluster-z"})
    try:
        with pytest.raises(HTTPException):
            await integrations.create_api_key(request=_request(), _=None, _rbac=PARTNER, db=None)
    finally:
        org_scope._allowed_clusters_var.reset(token)
    events = env["spy"].events
    assert len(events) == 1
    assert events[0]["result"] == "denied"
    assert events[0]["error_code"] == "cluster_out_of_scope"
    assert events[0]["target_id"] == "-"
    assert env["vault"].puts == []


async def test_a_project_bound_denial_is_recorded(env, monkeypatch):
    async def _refuse(*_a: Any, **_k: Any) -> None:
        raise HTTPException(status_code=403, detail="Not permitted to grant these projects")

    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", _refuse)
    with pytest.raises(HTTPException):
        await integrations.create_api_key(request=_request(), _=None, _rbac=PARTNER, db=None)
    assert [(e["result"], e["error_code"]) for e in env["spy"].events] == [("denied", "project_out_of_bound")]


async def test_update_records_before_and_after(env):
    permissions = [ClusterPermission(cluster_id="cluster-a", permissions=["write"], projects=["proj-a"])]
    await integrations.update_api_key_permissions(
        key_id="oak_test", permissions=permissions, _=None, _rbac=PARTNER, db=None
    )
    events = env["spy"].events
    assert [e["action"] for e in events] == ["integration.api_key.permissions.update"]
    event = events[0]
    assert event["target_id"] == "oak_test"
    assert event["before_state"] == {
        "cluster_permissions": [{"cluster_id": "cluster-a", "projects": ["*"], "permissions": ["read"]}]
    }
    assert event["after_state"] == {
        "cluster_permissions": [{"cluster_id": "cluster-a", "projects": ["proj-a"], "permissions": ["write"]}]
    }
    _clean(event)


async def test_revoke_records_one_event(env):
    await integrations.revoke_api_key(key_id="oak_test", _=None, _rbac=PARTNER)
    assert [(e["action"], e["target_id"]) for e in env["spy"].events] == [
        ("integration.api_key.revoke", "oak_test")
    ]
    _clean(env["spy"].events[0])


async def test_delete_records_one_event(env):
    await integrations.delete_api_key(key_id="oak_test", _=None, _rbac=PARTNER)
    assert [(e["action"], e["target_id"]) for e in env["spy"].events] == [
        ("integration.api_key.delete", "oak_test")
    ]
    _clean(env["spy"].events[0])


async def test_the_real_logger_writes_a_high_severity_identity_event(env, monkeypatch, caplog):
    monkeypatch.setattr(integrations, "activity_logger", ActivityLogger())
    caplog.set_level(logging.INFO, logger="orcastra.audit")
    await integrations.revoke_api_key(key_id="oak_test", _=None, _rbac=PARTNER)
    records = [r for r in caplog.records if r.name == "orcastra.audit"]
    assert len(records) == 1
    assert "AUDIT: integration.api_key.revoke on integration_api_key/oak_test" in records[0].getMessage()
    audit = records[0].audit_log_data
    assert audit["category"] == "identity"
    assert audit["severity"] == "high"
    assert audit["details"]["actor_email"] == "partner@example.test"
    _clean(audit)


async def test_an_audit_failure_never_fails_the_action(env, monkeypatch):
    class Broken:
        def log_activity(self, **_k: Any) -> str:
            raise RuntimeError("audit sink down")

    monkeypatch.setattr(integrations, "activity_logger", Broken())
    result = await integrations.revoke_api_key(key_id="oak_test", _=None, _rbac=PARTNER)
    assert result.is_revoked is True
