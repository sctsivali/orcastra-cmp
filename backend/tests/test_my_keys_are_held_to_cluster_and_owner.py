"""My-keys records are held to their cluster and, once they name one, to their owner.

A my-keys record is an LXD client certificate with its private key. Four gaps let it travel:
a record with no node skipped the cluster check on download and delete and was listed to
everyone, the list returned the full certificate fingerprint, and nothing tied a record to the
person who created it, so two partners in the same cluster scope listed, downloaded and deleted
each other's keys.

Records written before `owner_email` existed name nobody and there is nothing to backfill them
from, so they stay on cluster scope.
"""

import ast
import asyncio
import contextvars
import inspect
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import my_keys

PARTNER = AuthenticatedUser(
    user_id="partner-my-keys-probe", email="partner@example.test", groups=["role_partner"]
)
ADMIN = AuthenticatedUser(
    user_id="admin-my-keys-probe", email="admin@example.test", groups=["role_admin"]
)
FINGERPRINT = "ab" * 32


def _record(key_id: str, node: Optional[str], owner: Optional[str]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": key_id,
        "name": key_id,
        "project": "default",
        "auth_level": "standard",
        "created_at": "2026-01-01T00:00:00",
        "expires_at": "2099-01-01T00:00:00",
        "identity_id": f"{key_id}-identity",
        "group_name": f"{key_id}_group",
        "fingerprint": FINGERPRINT,
    }
    if node is not None:
        record["node"] = node
    if owner is not None:
        record["owner_email"] = owner
    return record


class _Vault:
    def __init__(self, record: Optional[dict[str, Any]]) -> None:
        self.record = record

    def vault_read(self, _path: str) -> Optional[dict[str, Any]]:
        return self.record


def _authorize(
    record: Optional[dict[str, Any]], caller: AuthenticatedUser, scope: Optional[set[str]]
) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        org_scope._allowed_clusters_var.set(scope)
        return my_keys._authorize_key(_Vault(record), "key-1", caller)

    return contextvars.copy_context().run(run)


def test_a_record_without_a_node_is_refused_even_to_an_admin() -> None:
    for caller, scope in ((PARTNER, {"cluster-a"}), (ADMIN, None)):
        with pytest.raises(HTTPException) as raised:
            _authorize(_record("key-1", None, None), caller, scope)
        assert raised.value.status_code == 404


def test_another_owners_key_is_refused_to_a_partner() -> None:
    with pytest.raises(HTTPException) as raised:
        _authorize(_record("key-1", "cluster-a", "other@example.test"), PARTNER, {"cluster-a"})
    assert raised.value.status_code == 404
    assert raised.value.detail == "Key not found"


def test_an_admin_reaches_another_owners_key() -> None:
    record = _record("key-1", "cluster-a", "other@example.test")
    assert _authorize(record, ADMIN, None) is record


def test_the_owner_and_a_legacy_record_inside_scope_pass() -> None:
    own = _record("key-1", "cluster-a", "partner@example.test")
    legacy = _record("key-2", "cluster-a", None)
    assert _authorize(own, PARTNER, {"cluster-a"}) is own
    assert _authorize(legacy, PARTNER, {"cluster-a"}) is legacy


def test_a_record_outside_scope_is_refused_by_the_cluster_gate() -> None:
    with pytest.raises(HTTPException) as raised:
        _authorize(_record("key-1", "cluster-z", "partner@example.test"), PARTNER, {"cluster-a"})
    assert raised.value.status_code == 403


class _Cache:
    def get(self, _namespace: str, _key: str) -> None:
        return None

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None


RECORDS = [
    _record("own", "cluster-a", "partner@example.test"),
    _record("legacy", "cluster-a", None),
    _record("other-owner", "cluster-a", "other@example.test"),
    _record("nodeless", None, "partner@example.test"),
    _record("out-of-scope", "cluster-z", "partner@example.test"),
]


@pytest.fixture
def vault_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: object())
    monkeypatch.setattr(my_keys, "list_keys_from_vault", lambda _client: [dict(r) for r in RECORDS])
    monkeypatch.setattr(my_keys, "get_cache_manager", lambda: _Cache())


def _list(caller: AuthenticatedUser, scope: Optional[set[str]]) -> Any:
    async def run() -> Any:
        org_scope._allowed_clusters_var.set(scope)
        return await my_keys.list_keys(status_filter=None, search=None, fresh=True, _rbac=caller)

    return asyncio.run(run())


def test_a_partner_lists_its_own_and_legacy_keys_only(vault_listing: None) -> None:
    listing = _list(PARTNER, {"cluster-a"})
    assert sorted(k.id for k in listing.keys) == ["legacy", "own"]


def test_an_admin_listing_skips_a_nodeless_record_instead_of_failing(vault_listing: None) -> None:
    listing = _list(ADMIN, None)
    assert sorted(k.id for k in listing.keys) == ["legacy", "other-owner", "out-of-scope", "own"]


def test_the_listed_fingerprint_is_truncated(vault_listing: None) -> None:
    listing = _list(PARTNER, {"cluster-a"})
    for key in listing.keys:
        assert key.fingerprint == f"{FINGERPRINT[:16]}..."
        assert len(key.fingerprint) == 19


def test_a_new_record_names_its_owner() -> None:
    source = Path(inspect.getfile(my_keys)).read_text(encoding="utf-8")
    create = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_key"
    )
    literal = next(
        node.value
        for node in ast.walk(create)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "key_metadata" for t in node.targets)
    )
    assert isinstance(literal, ast.Dict)
    keys = {k.value for k in literal.keys if isinstance(k, ast.Constant)}
    assert "owner_email" in keys
    owner_value = literal.values[[getattr(k, "value", None) for k in literal.keys].index("owner_email")]
    assert ast.unparse(owner_value) == "_rbac.email"
