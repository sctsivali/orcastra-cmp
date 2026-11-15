"""/validate and /whoami say what the key may do now, beside what is stored, and /validate refuses a
suspended add-on.

Both reported the stored grant. A key a partner created is held to its creator's projects on a
cluster another party holds, and a buyer founder's key to its paid term, so an integrator reading
`*` planned calls the server then refused. Both answers now carry `effective_cluster_permissions`:
the grants after the registration, party and paid-term narrowing every other door applies.
/validate also judged a suspended add-on's credential valid while every other door refused it.
"""

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.routers import integrations

STORED = [{"cluster_id": "held", "permissions": ["read", "write", "admin"], "projects": ["*"]}]


def _metadata(**extra: Any) -> Dict[str, Any]:
    return {"id": "oak_v", "app_name": "qa", "app_type": "orcahub", "is_active": True, "is_revoked": False,
            "expires_at": None, "created_by": "p@example.test", "cluster_permissions": list(STORED), **extra}


@pytest.fixture
def narrowed(monkeypatch):
    async def same(metadata):
        return metadata

    async def bound(metadata, **_kw):
        return {**metadata, "cluster_permissions": [{**STORED[0], "projects": ["default", "own"]}],
                "_held_cluster_permissions": STORED}

    async def nothing(*_a, **_k):
        return set()

    async def no_generations():
        return None

    monkeypatch.setattr(integrations, "_bound_to_registrations", same)
    monkeypatch.setattr(integrations, "narrow_to_party_bound", bound)
    monkeypatch.setattr(integrations, "narrow_to_paid_term", same)
    monkeypatch.setattr(integrations, "_unregistered_cluster_ids", nothing)
    monkeypatch.setattr(integrations, "_generations_for_scope", no_generations)
    monkeypatch.setattr(integrations, "_with_registration_state", lambda grants, _generations: list(grants))
    monkeypatch.setattr(integrations.cluster_generations, "reachable",
                        lambda grants, _generations: {g["cluster_id"] for g in grants})
    monkeypatch.setattr(integrations, "consume_api_key_budget", lambda key_id: True)
    monkeypatch.setattr(integrations, "verify_api_key_secret", lambda secret, stored: True)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())


async def test_validate_reports_the_effective_grant(narrowed, monkeypatch) -> None:
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k, use_cache=True: (_metadata(), "h"))
    answer = await integrations.validate_api_key_endpoint(
        integrations.APIKeyValidateRequest(api_key_id="oak_v", api_key_secret="s")
    )
    assert answer.valid is True
    assert [p.projects for p in answer.cluster_permissions] == [["*"]], "the stored grant is still reported"
    assert [p.projects for p in answer.effective_cluster_permissions] == [["default", "own"]]


async def test_whoami_reports_the_effective_grant(narrowed) -> None:
    key = {**_metadata(), "cluster_permissions": [{**STORED[0], "projects": ["default", "own"]}],
           "_held_cluster_permissions": STORED}
    answer = await integrations.whoami(api_key=key)
    assert [p.projects for p in answer.cluster_permissions] == [["*"]]
    assert [p.projects for p in answer.effective_cluster_permissions] == [["default", "own"]]


async def test_validate_refuses_a_suspended_add_on(narrowed, monkeypatch) -> None:
    from app.services import extension_scope

    async def suspended(_session, key_id):
        raise HTTPException(status_code=403, detail="This add-on installation is suspended")

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(extension_scope, "assert_install_is_live", suspended)
    monkeypatch.setattr("app.core.database.async_session_maker", lambda: _Session())
    monkeypatch.setattr(integrations, "get_api_key_from_vault",
                        lambda _v, _k, use_cache=True: (_metadata(extension_install_id=12), "h"))
    answer = await integrations.validate_api_key_endpoint(
        integrations.APIKeyValidateRequest(api_key_id="oak_v", api_key_secret="s")
    )
    assert answer.valid is False and "suspended" in (answer.error or "")
