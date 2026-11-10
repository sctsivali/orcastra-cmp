"""A key grant names only clusters that are registered, and a stale id is quiet and honest.

A key can still hold the id of a cluster that was deregistered or registered again under
another id. Polling it must not log a Vault 404 at ERROR every time, and raw-proxy answers 404
`cluster_not_registered` rather than 503 "Could not connect", which reads as an outage and
invites a retry loop. Grant writes check the registry, and deregistration removes the id from
key grants.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List

import pytest

from app.core.auth import AuthenticatedUser
from fastapi import HTTPException

from app.core.vault_client import VaultClientError
from app.routers import integrations
from app.services import key_grant_hygiene
from app.services.lxd import registry as cluster_registry
from app.services.lxd_certs.types import LXDCertManagerError

REGISTERED = frozenset({"nuc2", "bliss001"})


@pytest.fixture
def registry(monkeypatch):
    state: Dict[str, Any] = {"ids": REGISTERED, "fresh": []}

    async def registered(fresh: bool = False):
        state["fresh"].append(fresh)
        return state["ids"]

    monkeypatch.setattr(cluster_registry, "registered_cluster_ids", registered)
    return state


# ── reads ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stale_id_stays_in_what_the_key_reports_and_is_marked(registry) -> None:
    unregistered = await integrations._unregistered_cluster_ids(["nuc2", "nuc-2", "bliss01"])
    assert unregistered == frozenset({"nuc-2", "bliss01"})
    grant = {"cluster_id": "nuc-2", "permissions": ["read"], "projects": ["default"]}
    reported = integrations.ClusterPermission(**integrations._reported_grant(grant, unregistered))
    assert reported.model_dump()["registered"] is False
    kept = integrations.ClusterPermission(**integrations._reported_grant({"cluster_id": "nuc2"}, unregistered))
    assert "registered" not in kept.model_dump()


@pytest.mark.asyncio
async def test_an_unreadable_registry_marks_and_refuses_nothing(registry) -> None:
    registry["ids"] = None
    assert await integrations._unregistered_cluster_ids(["nuc-2"]) == frozenset()
    await integrations._refuse_a_cluster_not_registered("nuc-2")
    await integrations._refuse_new_grants_to_unregistered_clusters(["nuc-2"])


@pytest.mark.asyncio
async def test_the_proxies_answer_404_cluster_not_registered_not_an_outage(registry) -> None:
    await integrations._refuse_a_cluster_not_registered("nuc2")
    with pytest.raises(HTTPException) as refused:
        await integrations._refuse_a_cluster_not_registered("nuc-2")
    assert refused.value.status_code == 404
    assert refused.value.detail["code"] == "cluster_not_registered"


@pytest.mark.asyncio
async def test_a_fan_out_reports_a_stale_id_as_not_registered_without_looking_it_up(registry, monkeypatch) -> None:
    # A stale id is never looked up, so a /sync poll logs no Vault 404 for it.
    monkeypatch.setattr(integrations.health, "is_tripped", lambda cluster_id: False)
    fetched: List[str] = []

    def fetch(ctx: Any, cluster_id: str) -> List[Dict[str, Any]]:
        fetched.append(cluster_id)
        return [{"cluster_id": cluster_id}]

    fanout = await integrations._gather_per_cluster(fetch, None, {"nuc2", "nuc-2", "bliss01"}, "projects")
    assert fetched == ["nuc2"]
    assert fanout.items == [{"cluster_id": "nuc2"}]
    assert sorted(fanout.unreachable, key=lambda m: m["cluster_id"]) == [
        {"cluster_id": "bliss01", "reason": "not registered"},
        {"cluster_id": "nuc-2", "reason": "not registered"},
    ]

    registry["ids"] = None
    fetched.clear()
    fanout = await integrations._gather_per_cluster(fetch, None, ["nuc2", "nuc-2"], "projects")
    assert sorted(fetched) == ["nuc-2", "nuc2"], "an unreadable registry must not hide a cluster"
    assert fanout.unreachable == []


def test_both_proxy_doors_check_registration_right_after_the_grant() -> None:
    import inspect

    for route in (integrations.raw_proxy, integrations.proxy_operation):
        source = inspect.getsource(route)
        grant = source.index("assert_cluster_permission(api_key, request.cluster_id, required_permission)")
        check = source.index("await _refuse_a_cluster_not_registered(request.cluster_id)")
        assert grant < check, route.__name__


class _Vault:
    def __init__(self) -> None:
        self.reads: List[str] = []

    def vault_read(self, path: str) -> Dict[str, Any]:
        self.reads.append(path)
        if "nuc-2" in path:
            raise VaultClientError("404 Client Error: Not Found", status_code=404)
        if "broken" in path:
            raise VaultClientError("503 Server Error", status_code=503)
        return {"name": path.split("/")[-2]}


def _storage(vault: _Vault):
    from app.services.lxd_cert_manager import LXDCertificateManager

    manager = LXDCertificateManager.__new__(LXDCertificateManager)
    manager.vault = vault
    manager.clusters_path = "secret/data/clusters"
    return manager


def test_a_missing_registration_is_one_warning_per_process_not_an_error_per_poll(caplog) -> None:
    from app.services.lxd_certs import storage

    storage._absent_cluster_ids.discard("nuc-2")
    manager = _storage(_Vault())
    with caplog.at_level(logging.WARNING, logger=storage.logger.name):
        for _ in range(3):
            with pytest.raises(LXDCertManagerError):
                manager.get_cluster_info("nuc-2")
        with pytest.raises(LXDCertManagerError):
            manager.get_cluster_info("broken")
    rows = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert [level for level, message in rows if "nuc-2" in message] == ["WARNING"]
    assert [level for level, message in rows if "broken" in message] == ["ERROR"]


# ── grant writes ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_grant_to_an_unregistered_id_is_refused_from_a_fresh_registry_read(registry) -> None:
    with pytest.raises(HTTPException) as refused:
        await integrations._refuse_new_grants_to_unregistered_clusters(["nuc2", "nuc-9"])
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "cluster_not_registered"
    assert "nuc-9" in refused.value.detail["message"] and "bliss001" not in refused.value.detail["message"]
    assert registry["fresh"] == [True]


@pytest.mark.asyncio
async def test_an_id_the_key_already_holds_stays_editable(registry) -> None:
    await integrations._refuse_new_grants_to_unregistered_clusters(
        ["nuc2", "nuc-2"], already_held=frozenset({"nuc-2"})
    )


def test_both_grant_doors_check_the_registry() -> None:
    import inspect

    for route in (integrations.create_api_key, integrations.update_api_key_permissions):
        assert "_refuse_new_grants_to_unregistered_clusters(" in inspect.getsource(route), route.__name__


# ── cleanup ───────────────────────────────────────────────────────────────────────────────


def _metadata(*cluster_ids: str, add_on: bool = False) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "app_name": "orcahub",
        "cluster_permissions": [{"cluster_id": c, "permissions": ["admin"], "projects": ["*"]} for c in cluster_ids],
    }
    if add_on:
        metadata["extension_install_id"] = 124
    return metadata


def _stale(grant) -> bool:
    return grant.get("cluster_id") not in REGISTERED


def test_the_plan_drops_only_unregistered_ids_and_reports_what_it_will_not_touch() -> None:
    plan = key_grant_hygiene.plan_cleanup("oak_1", _metadata("nuc2", "nuc-2", "bliss01"), _stale)
    assert plan.drop == ["bliss01", "nuc-2"] and plan.writable
    assert key_grant_hygiene.plan_cleanup("oak_2", _metadata("nuc2"), _stale) is None
    add_on = key_grant_hygiene.plan_cleanup("key_3", _metadata("nuc2", "nuc-2", add_on=True), _stale)
    assert add_on.add_on and not add_on.writable
    only_stale = key_grant_hygiene.plan_cleanup("key_4", _metadata("nuc-2"), _stale)
    assert only_stale.left_empty and not only_stale.writable


class _KeyVault:
    def __init__(self, keys: Dict[str, Dict[str, Any]]):
        self.keys = keys
        self.writes: List[str] = []

    def vault_kv_put(self, path: str, metadata: Dict[str, Any]) -> None:
        key_id = path.split("/api_keys/")[1].split("/")[0]
        self.keys[key_id] = metadata
        self.writes.append(key_id)


class _Session:
    commits = 0

    async def commit(self) -> None:
        _Session.commits += 1


@pytest.fixture
def key_store(monkeypatch):
    from app.services import integration_key_ownership

    vault = _KeyVault({"oak_1": _metadata("nuc2", "nuc-2"), "oak_2": _metadata("nuc2"), "key_3": _metadata("nuc-2", "nuc2", add_on=True)})
    evicted: List[str] = []

    @asynccontextmanager
    async def lock(_db, _key_id):
        yield

    class _Cache:
        def delete_pattern(self, *_args):
            return None

    monkeypatch.setattr(key_grant_hygiene, "read_key_metadata", lambda _v, key_id: dict(vault.keys[key_id]))
    monkeypatch.setattr(key_grant_hygiene, "list_key_ids_strict", lambda _v: sorted(vault.keys))
    monkeypatch.setattr(integration_key_ownership, "serialize_key_ownership", lock)
    monkeypatch.setattr(integrations, "evict_api_key_cache", evicted.append)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(
        "app.services.activity_logger.get_activity_logger",
        lambda: type("L", (), {"log_activity": staticmethod(lambda **_k: None)})(),
    )
    return vault, evicted


@pytest.mark.asyncio
async def test_a_cleanup_rewrites_from_an_uncached_read_and_evicts_the_key(key_store) -> None:
    vault, evicted = key_store
    dropped = await key_grant_hygiene.apply_cleanup(_Session(), vault, "oak_1", _stale, reason="test")
    assert dropped == ["nuc-2"]
    assert [g["cluster_id"] for g in vault.keys["oak_1"]["cluster_permissions"]] == ["nuc2"]
    assert evicted == ["oak_1"]
    assert await key_grant_hygiene.apply_cleanup(_Session(), vault, "oak_1", _stale, reason="test") == []


@pytest.mark.asyncio
async def test_deregistration_strips_the_id_from_every_key_it_may_rewrite(key_store) -> None:
    vault, _ = key_store

    @asynccontextmanager
    async def sessions():
        yield _Session()

    changed = await key_grant_hygiene.strip_cluster_from_key_grants("nuc-2", session_maker=sessions)
    assert changed == 1
    assert vault.writes == ["oak_1"], "an add-on key keeps the grant its installation owns"
    assert any(g["cluster_id"] == "nuc-2" for g in vault.keys["key_3"]["cluster_permissions"])


def test_the_cluster_delete_route_strips_key_grants() -> None:
    import inspect

    from app.routers import cluster_registration

    assert "strip_cluster_from_key_grants(" in inspect.getsource(cluster_registration.delete_cluster)



def test_deregistration_strips_the_grants_of_the_registration_it_ended() -> None:
    import inspect

    from app.routers import cluster_registration

    source = inspect.getsource(cluster_registration.delete_cluster)
    assert source.index("end_registration(") < source.index("strip_cluster_from_key_grants(cluster_id, ended)")
    on = source.index("strip_cluster_from_key_grants(cluster_id, ended)")
    assert source.index("registered_cluster_ids(fresh=True)") > on, (
        "with generations on, the ended registration, not a registry read, says which grants go"
    )
    assert "asyncio.shield(cleanup)" in source, "a timeout must stop the wait, never the key rewrite"


def test_what_validate_reports_is_never_written_back_into_a_grant() -> None:
    import inspect

    for route in (integrations.create_api_key, integrations.update_api_key_permissions):
        assert 'model_dump(exclude={"registered"})' in inspect.getsource(route), route.__name__
    grant = integrations.ClusterPermissionRequest(cluster_id="nuc2", permissions=["read"], registered=False)
    assert "registered" not in grant.model_dump(exclude={"registered"})


@pytest.mark.asyncio
async def test_a_grant_edit_reads_what_the_key_holds_under_its_lock_and_lets_go_before_the_mappings(
    monkeypatch,
) -> None:
    # The held ids are read under the lock: a cleanup removing an id between an unlocked read and
    # the locked write would let the write put that id back, because it still counted as held.
    events: List[Any] = []
    stored = {
        "id": "oak_x", "name": "QA", "app_name": "QA", "created_at": "2026-10-24T00:00:00+00:00",
        "cluster_permissions": [{"cluster_id": "nuc2", "permissions": ["read"], "projects": ["*"]}],
    }

    @asynccontextmanager
    async def lock(_db, key_id):
        events.append("lock")
        yield
        events.append("unlock")

    async def authorize(*_a, **_k):
        return dict(stored)

    async def refuse(requested, already_held):
        events.append(("refuse", sorted(already_held)))

    async def commit(_db):
        events.append("commit")

    async def projects_exist(*_a, **_k):
        events.append("projects")

    class _Vault:
        def vault_kv_put(self, path, data):
            events.append("write")

    def read(_vault, key_id, use_cache=True):
        events.append(("read", use_cache))
        return dict(stored), "hash"

    def mappings(*_a):
        events.append("mappings")
        return []

    class _Cache:
        def delete_pattern(self, *_a):
            return None

    async def no_usage(keys):
        return keys

    monkeypatch.setattr(integrations, "serialize_key_ownership", lock)
    monkeypatch.setattr(integrations, "_authorize_api_key", authorize)
    monkeypatch.setattr(integrations, "refuse_grant_edit", lambda metadata: None)
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: None)
    monkeypatch.setattr(integrations, "_refuse_new_grants_to_unregistered_clusters", refuse)
    monkeypatch.setattr(integrations, "release_key_locks", commit)
    monkeypatch.setattr(integrations, "_assert_granted_projects_exist", projects_exist)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: _Vault())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", read)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", mappings)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda key_id: None)
    monkeypatch.setattr(integrations, "_with_usage", no_usage)

    async def stamped(requested, held=(), rebind=()):
        events.append(("stamp", sorted(g["cluster_id"] for g in held)))
        return list(requested)

    monkeypatch.setattr(integrations.cluster_generations, "stamped", stamped)

    request = [integrations.ClusterPermissionRequest(cluster_id="nuc2", permissions=["write"], projects=["*"])]
    await integrations.update_api_key_permissions(
        "oak_x", request, _=None,
        _rbac=AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"]),
        lxd_service=object(), db=object(),
    )

    assert events == [
        "projects", "lock", ("read", False), ("refuse", ["nuc2"]), ("stamp", ["nuc2"]), "write", "commit",
        "unlock", "mappings",
    ]
