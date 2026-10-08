"""A key write under the key's lock reads Vault itself, never the Redis mirror of it.

The metadata path is mirrored in the shared Redis cache. A reader that fetched the key just before
a revoke can refill the mirror with that copy after the revoke cleared it, and a read-modify-write
built from the mirror then writes the pre-revoke copy back: the key works again. The lock only
orders the writers; each one also has to read what the last of them wrote.

The behaviour tests send the real routes through a Vault client whose mirror holds such a copy.
The structural test finds every read inside a `serialize_key_ownership` block by shape.
"""

import ast
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from app.routers import integrations
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_an_add_on_credential_refuses_grant_edits import (
    PLAIN,
    installations,
    surface,  # noqa: F401
)
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import (
    ADMIN,
    KEYS,
    keys,  # noqa: F401
    send,
)

ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"
METADATA = _FakeVault._path(PLAIN, "metadata")


class _Mirrored:
    """The Vault client, with a mirror a reader refilled with a copy from before the last write."""

    def __init__(self, vault: Any, mirror: Dict[str, Dict[str, Any]]) -> None:
        self.vault = vault
        self.mirror = mirror

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if use_cache and path in self.mirror:
            return dict(self.mirror[path])
        return self.vault.vault_read(path)

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        # like the real client: a write drops the mirror entry for its path
        self.mirror.pop(path, None)
        self.vault.vault_kv_put(path, data)

    def vault_delete(self, path: str) -> None:
        self.vault.vault_delete(path)


@pytest.fixture
def mirrored(keys: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """keys, with PLAIN's mirror holding the copy from before the live document's last change."""
    vault = keys["vault"]
    keys["stale"] = dict(vault.store[METADATA])
    client = _Mirrored(vault, {METADATA: keys["stale"]})
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: client)
    keys["scope"](ADMIN)
    return keys


def live(mirrored: Dict[str, Any]) -> Dict[str, Any]:
    return mirrored["vault"].store[METADATA]


REVOKED = {"is_revoked": True, "is_active": False, "revoked_at": "2026-10-08T09:00:00"}


@pytest.mark.parametrize("route, body", [
    ("capabilities", {"capabilities": ["inventory.read", "lxd.proxy"]}),
    ("permissions", [{"cluster_id": "nuc1", "permissions": ["read"]}]),
], ids=["capabilities", "permissions"])
async def test_a_grant_edit_does_not_write_the_mirrors_pre_revoke_copy_back(
    mirrored: Dict[str, Any], route: str, body: Any
) -> None:
    live(mirrored).update(REVOKED)
    response = await send(ADMIN, installations(), "PUT", f"{KEYS}/{PLAIN}/{route}", body)
    assert response.status_code == 200, response.text
    assert mirrored["vault"].writes, "precondition: the edit wrote the key"
    assert (live(mirrored)["is_revoked"], live(mirrored)["is_active"]) == (True, False), "the revoke was undone"


async def test_a_revoke_does_not_write_the_mirrors_older_grant_back(mirrored: Dict[str, Any]) -> None:
    live(mirrored)["capabilities"] = ["inventory.read", "lxd.proxy"]
    response = await send(ADMIN, installations(), "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 200, response.text
    assert live(mirrored)["is_revoked"] is True
    assert live(mirrored)["capabilities"] == ["inventory.read", "lxd.proxy"], "the capability edit was undone"


async def test_a_rotation_is_judged_on_vault_not_the_mirror(mirrored: Dict[str, Any]) -> None:
    secret_before = dict(mirrored["vault"].store[_FakeVault._path(PLAIN, "secret")])
    live(mirrored).update(REVOKED)
    response = await send(ADMIN, installations(), "POST", f"{KEYS}/{PLAIN}/rotate-secret")
    assert response.status_code == 409, response.text
    assert mirrored["vault"].writes == [], "a revoked key was rotated from the mirror's copy"
    assert mirrored["vault"].store[_FakeVault._path(PLAIN, "secret")] == secret_before
    assert live(mirrored)["is_revoked"] is True


# note: by shape, so a new read under the lock cannot quietly go through the mirror

def _is_lock(item: ast.withitem) -> bool:
    call = item.context_expr
    return isinstance(call, ast.Call) and getattr(call.func, "id", None) == "serialize_key_ownership"


def _reads_the_key(call: ast.Call) -> bool:
    name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
    if name == "get_api_key_from_vault":
        return True
    return name in ("to_thread", "run_blocking") and bool(call.args) and (
        getattr(call.args[0], "id", None) == "get_api_key_from_vault"
    )


def _uncached(call: ast.Call) -> bool:
    return any(
        kw.arg == "use_cache" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in call.keywords
    )


def _helpers_run_under(block: ast.AsyncWith) -> Iterator[str]:
    for node in ast.walk(block):
        if isinstance(node, ast.Call) and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) in (
            "to_thread", "run_blocking",
        ) and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id != "get_api_key_from_vault":
            yield node.args[0].id


def test_every_read_under_the_key_lock_skips_the_mirror() -> None:
    tree = ast.parse(ROUTER.read_text())
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.AsyncWith) and any(map(_is_lock, node.items))]
    assert len(blocks) >= 5, "precondition: revoke, delete, both grant edits and the rotation take the lock"
    cached: List[str] = []
    for block in blocks:
        reads = [node for node in ast.walk(block) if isinstance(node, ast.Call) and _reads_the_key(node)]
        cached += [ast.unparse(call) for call in reads if not _uncached(call)]
        for helper in _helpers_run_under(block):
            body = functions.get(helper)
            if body is None:
                continue
            for call in ast.walk(body):
                if not isinstance(call, ast.Call):
                    continue
                callee = getattr(call.func, "id", None)
                if callee in ("get_api_key_from_vault", "_store_app_url_in_vault") and not _uncached(call):
                    cached.append(f"{helper}: {ast.unparse(call)}")
    assert not cached, "these reads under the key lock can return the Redis mirror's copy:\n  " + "\n  ".join(cached)
