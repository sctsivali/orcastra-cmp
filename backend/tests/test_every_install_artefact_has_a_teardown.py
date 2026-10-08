"""Uninstalling has to remove everything, and "everything" is longer than it looks.

An installation writes to Vault, to Redis, to per-process memory on four workers, and to two
Postgres tables that reference it only by a free-text id. Deleting the row is the part everyone
remembers. This deployment has already shipped the other shape twice: deleting a cluster reports
success while leaving rows in five tables, and one of those is a break-glass grant that then
authorizes into a re-registered cluster owned by somebody else.

So the artefacts are enumerated in one table in `services/extension_teardown.py`, and three
things are asserted here. That the teardown actually empties the stores, which is the evidence.
That an artefact recorded as deliberately kept is on a written list, so "kept" is a decision
rather than drift. And that a new Vault write cannot appear without somebody adding an entry for
it, which is the half that survives the next six months.
"""

import ast
import inspect
from typing import Any, Dict, List

import pytest

from app.services import extension_credentials as ec
from app.services import extension_settings as es
from app.services import extension_teardown as td

# Artefacts that outlive an uninstall on purpose. Anything else with no remover is an omission.
EXPECTED_KEPT = {
    "redis:api_key_revocation_tombstone",
    "redis:install_tombstone",
    "redis:rate_limit_bucket",
    "opensearch:audit_records",
}

# Every place the extension modules write to Vault. Growing this list without adding an artefact
# and a remover is the mistake this catches.
EXPECTED_VAULT_WRITERS = {
    "issue_install_credential",      # the credential, via save_api_key_to_vault
    "rotate_install_credential",     # the next generation, same
    "_retire_key",                   # shortens or revokes the superseded one
    "rotate_signing_secret",
    "store_secrets",                 # values for the manifest's secret fields
}

# Watching one module is how the settings store shipped with no teardown entry for two minutes.
WRITING_MODULES = (ec, es)


def test_an_artefact_kept_on_purpose_is_named_as_such() -> None:
    kept = set(td.artefacts_without_a_remover())
    assert kept == EXPECTED_KEPT, (
        "the set of artefacts that survive an uninstall changed. Adding one is a decision about "
        f"what a tenant can still see afterwards. unexpected={sorted(kept - EXPECTED_KEPT)}, "
        f"no longer kept={sorted(EXPECTED_KEPT - kept)}"
    )


def test_the_audit_trail_is_kept_deliberately() -> None:
    """Deleting it would erase the record a tenant reads to see what reached their instances."""
    assert td.INSTALL_ARTEFACTS["opensearch:audit_records"].remover is None
    assert "record that the installation existed" in td.INSTALL_ARTEFACTS[
        "opensearch:audit_records"
    ].note


def test_a_new_vault_write_forces_a_new_artefact_entry() -> None:
    """The structural half. No behavioural test can notice a write nobody thought to make."""
    writers = set()
    nodes = []
    for module in WRITING_MODULES:
        nodes.extend(ast.walk(ast.parse(inspect.getsource(module))))
    for node in nodes:
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            name = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
            if name in ("vault_kv_put", "save_api_key_to_vault"):
                writers.add(node.name)

    assert writers == EXPECTED_VAULT_WRITERS, (
        "the set of functions writing to Vault changed. Each one creates something an uninstall "
        f"has to remove. new={sorted(writers - EXPECTED_VAULT_WRITERS)}, "
        f"gone={sorted(EXPECTED_VAULT_WRITERS - writers)}"
    )


# ── the behavioural half ───────────────────────────────────────────────────────────────────────

class FakeVault:
    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}

    def vault_kv_put(self, path: str, data: Any) -> None:
        self.store[path] = data

    def vault_read(self, path: str, use_cache: bool = True) -> Any:
        if path not in self.store:
            raise KeyError(path)
        return self.store[path]

    def vault_delete(self, path: str) -> bool:
        self.store.pop(path, None)
        return True


class FakeCache:
    def __init__(self) -> None:
        self.store: Dict[str, Any] = {}

    def set(self, namespace: str, key: str, value: Any, ttl: int = 0) -> None:
        self.store[f"{namespace}:{key}"] = value

    def get(self, namespace: str, key: str) -> Any:
        return self.store.get(f"{namespace}:{key}")

    def delete(self, namespace: str, key: str) -> None:
        self.store.pop(f"{namespace}:{key}", None)

    def delete_pattern(self, namespace: str, pattern: str = "*") -> None:
        prefix = f"{namespace}:{pattern.rstrip('*')}"
        for key in [k for k in self.store if k.startswith(prefix)]:
            self.store.pop(key, None)


class FakeDB:
    def __init__(self) -> None:
        self.statements: List[str] = []

    async def execute(self, statement, params=None):
        self.statements.append(" ".join(str(statement).split()))
        return None


@pytest.fixture
def torn_down(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    vault, cache, db = FakeVault(), FakeCache(), FakeDB()

    monkeypatch.setattr(ec, "evict_api_key_cache", lambda _k: None)
    monkeypatch.setattr(td, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(
        "app.services.extension_scope.get_cache_manager", lambda: cache
    )
    monkeypatch.setattr(
        "app.routers.integrations.delete_api_key_from_vault",
        lambda client, key_id: [
            client.vault_delete(f"secret/data/integrations/api_keys/{key_id}/metadata"),
            client.vault_delete(f"secret/data/integrations/api_keys/{key_id}/secret"),
        ],
    )
    monkeypatch.setattr(ec, "delete_api_key_from_vault", lambda client, key_id: [
        client.vault_delete(f"secret/data/integrations/api_keys/{key_id}/metadata"),
        client.vault_delete(f"secret/data/integrations/api_keys/{key_id}/secret"),
    ])

    key_id, _secret, _signing, _meta = ec.issue_install_credential(
        vault, extension_slug="acme-backup", extension_name="Acme Backup", install_id=7,
        organization_id=3, organization_slug="acme", capabilities=["inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc2", "permissions": ["read"]}],
        created_by="ops@example.com", expire_days=365,
    )
    # A cached descriptor and a cached key list, as a live deployment would have.
    cache.set("vault", f"secret/data/integrations/api_keys/{key_id}/metadata", {"cached": True})
    cache.set("api_responses", "integrations:api_keys:list:all", [{"id": key_id}])

    before = dict(vault.store)
    return vault, cache, db, key_id, before


def test_an_uninstall_leaves_nothing_in_vault(torn_down) -> None:
    import asyncio

    vault, cache, db, key_id, before = torn_down
    assert any(key_id in path for path in before), "the fixture did not create anything"

    asyncio.run(td.teardown_install(db, vault, install_id=7, api_key_id=key_id))

    survivors = [path for path in vault.store if key_id in path or "installations/7" in path]
    assert survivors == [], f"uninstall left Vault entries behind: {survivors}"


def test_an_uninstall_clears_the_caches_that_would_still_answer(torn_down) -> None:
    import asyncio

    vault, cache, db, key_id, _before = torn_down
    asyncio.run(td.teardown_install(db, vault, install_id=7, api_key_id=key_id))

    stale = [k for k in cache.store if k.startswith("vault:") or k.startswith("api_responses:")]
    assert stale == [], f"a cached answer outlived the installation: {stale}"
    # The tombstone is the one entry that must remain: its whole job is to outlive the row so
    # the other three workers drop their own copies.
    assert cache.get("extension_install_revoked", key_id) is not None


def test_an_uninstall_reaches_the_rows_that_reference_only_a_free_text_key(torn_down) -> None:
    """Upload sessions and break-glass grants name the key with no foreign key to enforce it."""
    import asyncio

    vault, _cache, db, key_id, _before = torn_down
    asyncio.run(td.teardown_install(db, vault, install_id=7, api_key_id=key_id))

    joined = " | ".join(db.statements)
    assert "image_upload_sessions" in joined
    assert "iso_upload_sessions" in joined
    assert "break_glass_grants" in joined
    assert "DELETE FROM extension_installations" in joined


def test_the_row_is_deleted_last(torn_down) -> None:
    """Order is chosen so the surviving failure mode is denial, not continued access.

    Vault first means a half-finished teardown produces a 401 and a bug report. The other order
    leaves a live credential with nothing describing it, which nobody notices.
    """
    import asyncio

    vault, _cache, db, key_id, _before = torn_down
    asyncio.run(td.teardown_install(db, vault, install_id=7, api_key_id=key_id))
    assert "DELETE FROM extension_installations" in db.statements[-1]


def test_a_superseded_credential_is_removed_too(torn_down) -> None:
    """A rotation in flight leaves two live keys, and the older one is easy to forget."""
    import asyncio

    vault, _cache, db, key_id, _before = torn_down
    old_id, _s, _m = ec.rotate_install_credential(
        vault, old_key_id=key_id, extension_slug="acme-backup", extension_name="Acme Backup",
        install_id=7, organization_id=3, organization_slug="acme",
        capabilities=["inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc2", "permissions": ["read"]}],
        created_by="ops@example.com", expire_days=365, credential_generation=1,
        grace_seconds=600,
    )
    asyncio.run(
        td.teardown_install(db, vault, install_id=7, api_key_id=old_id, previous_api_key_id=key_id)
    )
    survivors = [p for p in vault.store if key_id in p or old_id in p]
    assert survivors == [], f"a superseded credential survived the uninstall: {survivors}"
