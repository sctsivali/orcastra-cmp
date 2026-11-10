"""A rescope and the reconcile sweep decide on the locked installation row and reach every live credential.

With an unlocked read of the installation row, a rotation in between leaves the new key with a stale
scope, which the sweep cannot heal because it compares the row with itself, and the same race can
restore a retired key. Both lock the row (lock_installation), lock every credential that still
authenticates (live_credentials: the current key and the previous one inside its grace window), read
each uncached, and write the grant onto all of them. The sweep works from plain candidate values, so
one failed row does not end the pass.
"""

import ast
import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List

import pytest
from fastapi import HTTPException

from app.core.api_key_expiry import now_utc
from app.routers import extension_installs, integrations
from app.services import extension_credential_guard, extension_credentials, extension_install
from app.services import extension_reconcile as sweep
from tests.test_a_capability_grant_leaves_a_record import SECRETS, _FakeVault
from tests.test_an_add_on_credential_grant_leaves_a_record import (  # noqa: F401 - fixture
    DIGEST,
    KEY,
    PARTNER,
    _DB,
    _grant,
    _install,
    surface,
)
from tests.test_the_reconcile_sweep_narrows_and_reclaims import CredentialStore, FakeInstall, ScopeDB, lock_from

OLD = "oak_addon_previous"


def with_key(vault: _FakeVault, key_id: str, **fields: Any) -> _FakeVault:
    vault.store[_FakeVault._path(key_id, "metadata")] = {"id": key_id, **fields}
    vault.store[_FakeVault._path(key_id, "secret")] = {"hash": SECRETS[1], "raw": SECRETS[0]}
    return vault


def grant_of(vault: _FakeVault, key_id: str) -> List[Dict[str, Any]]:
    return vault.store[_FakeVault._path(key_id, "metadata")]["cluster_permissions"]


def in_grace(record: Any, minutes: int) -> Any:
    record.previous_api_key_id = OLD
    record.previous_key_expires_at = now_utc() + timedelta(minutes=minutes)
    return record


# live_credentials

def test_the_previous_key_is_live_only_inside_its_grace_window() -> None:
    record = SimpleNamespace(api_key_id=KEY, previous_api_key_id=OLD,
                             previous_key_expires_at=now_utc() + timedelta(minutes=5))
    assert extension_credential_guard.live_credentials(record) == sorted([KEY, OLD])
    record.previous_key_expires_at = now_utc() - timedelta(seconds=1)
    assert extension_credential_guard.live_credentials(record) == [KEY]
    record.previous_key_expires_at = (now_utc() + timedelta(minutes=5)).replace(tzinfo=None)
    assert extension_credential_guard.live_credentials(record) == sorted([KEY, OLD]), "a naive time is UTC"
    assert extension_credential_guard.live_credentials(SimpleNamespace(api_key_id=KEY)) == [KEY]


# The grant rewrite

def test_a_narrowing_reaches_the_previous_key_inside_its_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    vault = with_key(_FakeVault({"id": KEY, "capabilities": [], "cluster_permissions": [_grant(), _grant("lost")]}),
                     OLD, capabilities=[], cluster_permissions=[_grant(), _grant("lost")], is_revoked=False,
                     expires_at="2099-01-01T00:00:00+00:00")
    reads: List[Any] = []
    real = vault.vault_read

    def _read(path: str, use_cache: bool = True) -> Dict[str, Any]:
        if path.endswith("/metadata"):  # The secret path is never mirrored.
            reads.append(use_cache)
        return real(path, use_cache)

    vault.vault_read = _read
    extension_install._rewrite_key_grant(vault, in_grace(_install(["inventory.read"]), 5))
    assert grant_of(vault, KEY) == [_grant()] and grant_of(vault, OLD) == [_grant()]
    assert reads and not any(reads), "a whole-document write was built from a cached read"
    assert vault.store[_FakeVault._path(OLD, "metadata")]["expires_at"] == "2099-01-01T00:00:00+00:00"


def test_a_previous_key_past_its_grace_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    vault = with_key(_FakeVault({"id": KEY, "capabilities": [], "cluster_permissions": [_grant()]}),
                     OLD, capabilities=[], cluster_permissions=[_grant("lost")])
    extension_install._rewrite_key_grant(vault, in_grace(_install(["inventory.read"]), -1))
    assert grant_of(vault, OLD) == [_grant("lost")]


def test_a_revoked_previous_key_stays_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    vault = with_key(_FakeVault({"id": KEY, "capabilities": [], "cluster_permissions": [_grant()]}),
                     OLD, capabilities=[], cluster_permissions=[_grant()], is_revoked=True, is_active=False)
    extension_install._rewrite_key_grant(vault, in_grace(_install(["inventory.read"]), 5))
    old = vault.store[_FakeVault._path(OLD, "metadata")]
    assert old["is_revoked"] is True and old["is_active"] is False


def test_a_previous_key_already_deleted_does_not_block_the_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    vault = _FakeVault({"id": KEY, "capabilities": [], "cluster_permissions": [_grant(), _grant("lost")]})
    record = in_grace(_install(["inventory.read"]), 5)
    record.granted_scope = [_grant()]
    extension_install._rewrite_key_grant(vault, record)
    assert grant_of(vault, KEY) == [_grant()]


def test_the_retirement_reads_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Any] = []

    def _read(_client: Any, key_id: str, *, use_cache: bool = True) -> Any:
        seen.append(use_cache)
        return {"id": key_id, "expires_at": None, "is_revoked": False}, "hash"

    monkeypatch.setattr(extension_credentials, "get_api_key_from_vault", _read)
    monkeypatch.setattr(extension_credentials, "evict_api_key_cache", lambda _k: None)
    extension_credentials._retire_key(SimpleNamespace(vault_kv_put=lambda *_a: None), OLD, grace_seconds=0)
    assert seen == [False]


# Rescope

async def test_a_rescope_locks_the_row_then_every_live_key_then_writes(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: List[str] = []
    vault = surface["use"](with_key(
        _FakeVault({"id": KEY, "capabilities": ["inventory.read"], "cluster_permissions": [_grant()],
                    "extension_install_id": 11}),
        OLD, capabilities=["inventory.read"], cluster_permissions=[_grant()], extension_install_id=11,
    ))
    record = in_grace(_install(["inventory.read"]), 5)
    surface["load"](record)

    async def _locked(_db: Any, install: Any) -> Any:
        events.append("row")
        return install

    real_key_lock = extension_install.serialize_key_ownership

    @asynccontextmanager
    async def _key(db: Any, key_id: str) -> AsyncIterator[None]:
        events.append(f"key:{key_id}")
        async with real_key_lock(db, key_id):
            yield

    real_rewrite = extension_install._rewrite_key_grant

    def _rewrite(client: Any, install: Any) -> Dict[str, Any]:
        events.append("rewrite")
        return real_rewrite(client, install)

    monkeypatch.setattr(extension_install, "lock_installation", _locked)
    monkeypatch.setattr(extension_install, "serialize_key_ownership", _key)
    monkeypatch.setattr(extension_install, "_rewrite_key_grant", _rewrite)
    request = SimpleNamespace(consent=SimpleNamespace(version="1.0.0", manifest_digest=DIGEST),
                              capabilities=["inventory.read", "lxd.proxy"], cluster_permissions=[_grant()])
    await extension_installs.rescope(11, request, None, PARTNER, _DB())
    assert events == ["row", *[f"key:{k}" for k in sorted([KEY, OLD])], "rewrite"]
    for key_id in (KEY, OLD):
        assert vault.store[_FakeVault._path(key_id, "metadata")]["capabilities"] == ["inventory.read", "lxd.proxy"]


# The reconcile sweep

def _sweep(db: ScopeDB, monkeypatch: pytest.MonkeyPatch, store: CredentialStore, locked: List[Any]) -> int:
    async def _bound(_db: Any, organization_id: int) -> set:
        return db.bindings.get(organization_id, set())

    async def _owns_nothing(_db: Any, _key: str) -> List[str]:
        return []

    monkeypatch.setattr(sweep, "_bound_clusters", _bound)
    monkeypatch.setattr(sweep, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(sweep.activity_logger, "log_activity", lambda **_kw: None)
    monkeypatch.setattr(sweep, "_rewrite_key_grant", store.rewrite)
    monkeypatch.setattr(sweep, "revoke_install_credential", store.revoke)
    monkeypatch.setattr(sweep, "_owned_by", _owns_nothing)
    monkeypatch.setattr(sweep, "lock_installation_id", lock_from(locked))
    return asyncio.run(sweep._narrow_drifted_scope(db, object()))


def test_a_row_uninstalled_meanwhile_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = FakeInstall(id=1, organization_id=1, api_key_id="k1", granted_scope=[{"cluster_id": "lost"}, {"cluster_id": "a"}])
    kept = FakeInstall(id=2, organization_id=1, api_key_id="k2", granted_scope=[{"cluster_id": "lost"}, {"cluster_id": "a"}])
    store = CredentialStore()
    db = ScopeDB(installs=[gone, kept], bindings={1: {"a"}})
    assert _sweep(db, monkeypatch, store, [kept]) == 1
    assert list(store.rewritten) == ["k2"]


def test_the_row_read_under_the_lock_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = FakeInstall(id=1, organization_id=1, api_key_id="k-old",
                       granted_scope=[{"cluster_id": "a"}, {"cluster_id": "lost"}])
    # A rotation committed meanwhile, and a rescope already narrowed the row.
    locked = FakeInstall(id=1, organization_id=1, api_key_id="k-new", granted_scope=[{"cluster_id": "a"}])
    store = CredentialStore()
    assert _sweep(ScopeDB(installs=[seen], bindings={1: {"a"}}), monkeypatch, store, [locked]) == 0
    assert store.rewritten == {} and store.revoked == []


def test_a_total_loss_revokes_every_live_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    record = FakeInstall(id=1, organization_id=1, api_key_id="k-new", granted_scope=[{"cluster_id": "lost"}],
                         previous_api_key_id="k-old")
    record.previous_key_expires_at = now_utc() + timedelta(minutes=5)
    store = CredentialStore()
    assert _sweep(ScopeDB(installs=[record], bindings={1: set()}), monkeypatch, store, [record]) == 1
    assert sorted(store.revoked) == ["k-new", "k-old"] and record.status == "suspended"


def test_a_narrowing_rewrites_the_key_the_locked_row_names(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = FakeInstall(id=1, organization_id=1, api_key_id="k-old",
                       granted_scope=[{"cluster_id": "a"}, {"cluster_id": "lost"}])
    locked = FakeInstall(id=1, organization_id=1, api_key_id="k-new",
                         granted_scope=[{"cluster_id": "a"}, {"cluster_id": "lost"}])
    store = CredentialStore()
    assert _sweep(ScopeDB(installs=[seen], bindings={1: {"a"}}), monkeypatch, store, [locked]) == 1
    assert list(store.rewritten) == ["k-new"], "the retired key was rewritten"


# Lock and read ordering in the source

def _first_calls(module: Any, function_name: str) -> Dict[str, int]:
    tree = ast.parse(inspect.getsource(module))
    function = next(n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                    and n.name == function_name)
    lines: Dict[str, int] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name:
                lines[name] = min(lines.get(name, node.lineno), node.lineno)
    return lines


def test_a_rescope_locks_before_it_rewrites() -> None:
    lines = _first_calls(extension_install, "rescope_install")
    assert lines["lock_installation"] < lines["serialize_key_ownership"] < lines["_rewrite_key_grant"]


def test_the_sweep_locks_before_it_rewrites_or_revokes() -> None:
    lines = _first_calls(sweep, "_narrow_drifted_scope")
    lock = lines["lock_installation_id"]
    assert lock < lines["serialize_key_ownership"] < min(lines["_rewrite_key_grant"], lines["revoke_install_credential"])


@pytest.mark.parametrize("module, function_name", [(extension_install, "_rewrite_key_grant"),
                                                   (extension_credentials, "_retire_key")])
def test_a_whole_key_rewrite_reads_uncached(module: Any, function_name: str) -> None:
    source = inspect.getsource(getattr(module, function_name))
    assert "get_api_key_from_vault(" in source and "use_cache=False" in source


def test_a_lookup_answers_404_once_the_row_is_gone() -> None:
    with pytest.raises(HTTPException) as gone:
        asyncio.run(lock_from([])(None, 1))
    assert gone.value.status_code == 404
