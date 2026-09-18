"""A policy rule may only name a cluster that is registered.

The OrcaHub-facing writers stored `cluster_id` verbatim; the only check was the API key's own
grant, which is validated when the key is minted and can carry an id no cluster has. The dev
installation held 25 rules under `nuc-2`, `pandora-semanan` and `blitz01` after those clusters
were registered again as `nuc2`, `pandora` and `blitz001`, and every one of them counted for
nothing in the Partner bound and the tenant ACL. The writers now refuse at the boundary.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.routers.integrations import (
    ProvisionTenantAccessRequest,
    SyncPoliciesRequest,
    provision_tenant_access,
    sync_policies_from_external,
)
from app.services.lxd import registry as cluster_registry


def _key(cluster_id: str) -> Dict[str, Any]:
    return {
        "id": "key-1",
        "created_by": "ops@example.com",
        "cluster_permissions": [{"cluster_id": cluster_id, "projects": ["*"]}],
    }


def _provision(cluster_id: str) -> ProvisionTenantAccessRequest:
    return ProvisionTenantAccessRequest(
        organization_external_id="00000000-0000-0000-0000-000000000000",
        cluster_id=cluster_id,
        project_name="default",
        user_emails=["tenant@example.com"],
    )


class _RecordingSession:
    """Fails the test if the handler writes anything."""

    def __init__(self) -> None:
        self.added: List[Any] = []
        self.commits = 0

    async def execute(self, *_args: Any, **_kwargs: Any):
        raise AssertionError("the registry gate must run before the database is read")

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


def _registry(monkeypatch, value: Optional[frozenset], calls: Optional[List[int]] = None) -> None:
    async def _registered():
        if calls is not None:
            calls.append(1)
        return value

    monkeypatch.setattr(cluster_registry, "registered_cluster_ids", _registered)


def test_provision_refuses_a_cluster_no_registration_knows(monkeypatch) -> None:
    _registry(monkeypatch, frozenset({"nuc2"}))
    db = _RecordingSession()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(provision_tenant_access(body=_provision("nuc-2"), api_key=_key("nuc-2"), db=db))

    assert exc.value.status_code == 404
    assert "nuc-2" in str(exc.value.detail)
    # The message names the caller's input, never the fleet.
    assert "nuc2" not in str(exc.value.detail).replace("nuc-2", "")
    assert db.added == [] and db.commits == 0


def test_provision_is_a_503_while_the_registry_cannot_be_read(monkeypatch) -> None:
    _registry(monkeypatch, None)
    db = _RecordingSession()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(provision_tenant_access(body=_provision("nuc2"), api_key=_key("nuc2"), db=db))

    assert exc.value.status_code == 503
    assert db.added == []


def test_the_key_scope_check_still_runs_before_the_registry(monkeypatch) -> None:
    calls: List[int] = []
    _registry(monkeypatch, frozenset({"nuc2"}), calls)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(provision_tenant_access(body=_provision("nuc2"), api_key=_key("other"), db=_RecordingSession()))

    assert exc.value.status_code == 403
    assert calls == [], "the registry must not be consulted for a cluster the key does not hold"


def test_a_registered_cluster_passes_the_gate(monkeypatch) -> None:
    """Past the gate the handler reads the session; the recording session's execute raising
    is the proof that the gate let a registered id through."""
    _registry(monkeypatch, frozenset({"nuc2"}))

    with pytest.raises(AssertionError, match="registry gate"):
        asyncio.run(provision_tenant_access(body=_provision("nuc2"), api_key=_key("nuc2"), db=_RecordingSession()))


def _sync_body(*cluster_ids: str) -> SyncPoliciesRequest:
    return SyncPoliciesRequest.model_validate(
        {
            "policies": [
                {
                    "external_id": f"policy-{i}",
                    "name": f"auto:project-{i}",
                    "org_external_id": "org-1",
                    "rules": [{"cluster_id": cid, "project_name": f"project-{i}"}],
                }
                for i, cid in enumerate(cluster_ids)
            ]
        }
    )


class _SyncSession(_RecordingSession):
    """Answers the org lookup with nothing, so a policy that passes the gate is reported as
    'organization not found' rather than written; what matters here is which policies reach
    that lookup."""

    def __init__(self) -> None:
        super().__init__()
        self.lookups = 0

    async def execute(self, *_args: Any, **_kwargs: Any):
        self.lookups += 1

        class _NoResult:
            def scalar_one_or_none(self):
                return None

        return _NoResult()


def test_sync_skips_a_policy_naming_an_unregistered_cluster_and_keeps_the_rest(monkeypatch) -> None:
    _registry(monkeypatch, frozenset({"nuc2"}))
    db = _SyncSession()

    result = asyncio.run(sync_policies_from_external(body=_sync_body("nuc-2", "nuc2"), api_key=_key("nuc2"), db=db))

    errors = result.get("errors") if isinstance(result, dict) else getattr(result, "errors", [])
    assert any("nuc-2" in e and "auto:project-0" in e for e in errors), errors
    # The second policy reached the organization lookup; the first never did.
    assert db.lookups == 1


def test_sync_is_a_503_before_any_policy_is_touched_when_the_registry_is_unreadable(monkeypatch) -> None:
    _registry(monkeypatch, None)
    db = _SyncSession()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(sync_policies_from_external(body=_sync_body("nuc2"), api_key=_key("nuc2"), db=db))

    assert exc.value.status_code == 503
    assert db.lookups == 0 and db.commits == 0
