"""The sweep that closes the window the request path deliberately leaves open.

`extension_scope` refuses a suspended or missing installation from a cached snapshot, and skips
the two-table join that decides whether the installing partner still reaches every cluster in the
grant. Its own docstring says that drift is left to a reconcile sweep. For a while the sweep did
not exist: the config knob, both audit actions and the join key in the credential metadata were
all present, with no implementation and nothing scheduling one. So the stored grant was trusted
indefinitely, which on a platform where clusters are rented means one tenant's add-on reading the
next tenant's workload after the hardware changed hands.

These assert both passes do their work AND that they can tell there was work to do. A sweep that
returns a confident zero because it looked in the wrong place is indistinguishable from a clean
tree, so every case here pairs the change it expects with a case that must not change.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import pytest

from app.services import extension_reconcile as sweep


@dataclass
class FakeInstall:
    id: int
    organization_id: int
    api_key_id: str
    granted_scope: List[Dict[str, Any]]
    status: str = "active"
    previous_api_key_id: Optional[str] = None
    suspend_reason: Optional[str] = None


@dataclass
class FakeResult:
    rows: List[Any]

    def scalars(self):
        return self

    def all(self):
        return self.rows


@dataclass
class ScopeDB:
    """Answers the scope pass's one query. Kept separate from the orphan pass's fake because
    `select(ExtensionInstallation)` renders every column, `previous_api_key_id` among them, so a
    single fake dispatching on the statement text matched the wrong branch."""

    installs: List[FakeInstall]
    bindings: Dict[int, Set[str]]
    commits: int = 0

    async def execute(self, _statement):
        return FakeResult([i for i in self.installs if i.status == "active"])

    async def commit(self):
        self.commits += 1


@dataclass
class OrphanDB:
    installs: List[FakeInstall]
    commits: int = 0

    async def execute(self, _statement):
        return FakeResult([(i.api_key_id, i.previous_api_key_id) for i in self.installs])

    async def commit(self):
        self.commits += 1


def _run_scope_pass(db: ScopeDB, monkeypatch) -> int:
    async def _bound(_db, organization_id: int):
        return db.bindings.get(organization_id, set())

    monkeypatch.setattr(sweep, "_bound_clusters", _bound)
    monkeypatch.setattr(sweep, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(sweep.activity_logger, "log_activity", lambda **_kw: None)
    return asyncio.run(sweep._narrow_drifted_scope(db))


def test_a_grant_is_trimmed_to_what_the_organization_still_reaches(monkeypatch) -> None:
    install = FakeInstall(
        id=1, organization_id=3, api_key_id="k1",
        granted_scope=[{"cluster_id": "nuc2"}, {"cluster_id": "sold-on"}],
    )
    db = ScopeDB(installs=[install], bindings={3: {"nuc2"}})

    assert _run_scope_pass(db, monkeypatch) == 1
    assert install.granted_scope == [{"cluster_id": "nuc2"}]
    assert install.status == "active", "it still reaches something, so it must not be suspended"


def test_an_installation_left_reaching_nothing_is_suspended(monkeypatch) -> None:
    install = FakeInstall(
        id=2, organization_id=3, api_key_id="k2", granted_scope=[{"cluster_id": "sold-on"}],
    )
    db = ScopeDB(installs=[install], bindings={3: set()})

    assert _run_scope_pass(db, monkeypatch) == 1
    assert install.granted_scope == []
    assert install.status == "suspended"
    assert "no longer reaches" in (install.suspend_reason or "")


def test_a_grant_that_still_fits_is_left_alone(monkeypatch) -> None:
    """The pass must be able to do nothing. Without this a sweep that rewrote every row would
    look identical to one that correctly found no drift."""
    install = FakeInstall(
        id=3, organization_id=3, api_key_id="k3", granted_scope=[{"cluster_id": "nuc2"}],
    )
    db = ScopeDB(installs=[install], bindings={3: {"nuc2", "pandora"}})

    assert _run_scope_pass(db, monkeypatch) == 0
    assert db.commits == 0, "an unchanged grant must not write"
    assert install.granted_scope == [{"cluster_id": "nuc2"}]


# ── the orphan pass ────────────────────────────────────────────────────────────────────────────

class FakeVault:
    def __init__(self, keys: List[Dict[str, Any]]) -> None:
        self.keys = keys
        self.revoked: List[str] = []


def _run_orphan_pass(installs, vault_keys, monkeypatch) -> int:
    db = OrphanDB(installs=installs)
    vault = FakeVault(vault_keys)

    monkeypatch.setattr(
        "app.routers.integrations.list_api_keys_from_vault", lambda _c: vault.keys
    )
    monkeypatch.setattr(
        sweep, "revoke_install_credential", lambda _c, key_id: vault.revoked.append(key_id)
    )
    monkeypatch.setattr(sweep.activity_logger, "log_activity", lambda **_kw: None)
    count = asyncio.run(sweep._revoke_orphaned_credentials(db, vault))
    return count, vault


def test_a_credential_no_installation_claims_is_revoked(monkeypatch) -> None:
    """Teardown writes Vault before deleting the row, so an interrupted uninstall leaves exactly
    this: a key that still authenticates with nothing left to say whose it was."""
    count, vault = _run_orphan_pass(
        installs=[FakeInstall(id=1, organization_id=3, api_key_id="live", granted_scope=[])],
        vault_keys=[
            {"id": "live", "extension_install_id": 1},
            {"id": "orphan", "extension_install_id": 99},
        ],
        monkeypatch=monkeypatch,
    )
    assert count == 1
    assert vault.revoked == ["orphan"]


def test_an_ordinary_key_is_never_touched(monkeypatch) -> None:
    """A key with no install marker belongs to the integrations surface, not to an add-on."""
    count, vault = _run_orphan_pass(
        installs=[],
        vault_keys=[{"id": "someone-elses-key", "name": "OrcaHub"}],
        monkeypatch=monkeypatch,
    )
    assert count == 0
    assert vault.revoked == []


def test_a_superseded_key_inside_its_grace_window_survives(monkeypatch) -> None:
    """Rotation retires the old key by shortening its expiry, so it is still legitimately usable
    and is not an orphan. Revoking it here would break the overlap rotation exists to provide."""
    count, vault = _run_orphan_pass(
        installs=[
            FakeInstall(
                id=1, organization_id=3, api_key_id="new", granted_scope=[],
                previous_api_key_id="old",
            )
        ],
        vault_keys=[
            {"id": "new", "extension_install_id": 1},
            {"id": "old", "extension_install_id": 1},
        ],
        monkeypatch=monkeypatch,
    )
    assert count == 0
    assert vault.revoked == []


def test_an_already_revoked_orphan_is_not_revoked_twice(monkeypatch) -> None:
    """The sweep runs on every worker with no leader lock, so it has to be idempotent."""
    count, vault = _run_orphan_pass(
        installs=[],
        vault_keys=[{"id": "gone", "extension_install_id": 7, "is_revoked": True}],
        monkeypatch=monkeypatch,
    )
    assert count == 0
    assert vault.revoked == []


def test_the_marker_field_is_the_one_the_credential_actually_writes() -> None:
    """Guards the failure this pass is most likely to have: reading a field name that does not
    exist makes it return zero for ever, and a zero is what a clean tree looks like."""
    from app.services.extension_credentials import build_install_metadata

    metadata = build_install_metadata(
        key_id="abc",
        extension_slug="acme",
        extension_name="Acme",
        organization_id=3,
        organization_slug="acme-org",
        install_id=42,
        capabilities=["inventory.read"],
        cluster_permissions=[],
        created_by="someone@example.com",
        expire_days=365,
        credential_generation=1,
    )
    assert metadata["extension_install_id"] == 42, "the join key the sweep filters on"
    assert metadata["id"] == "abc", "the sweep reads 'id'; there is no 'key_id' field"


def test_the_sweep_is_actually_scheduled() -> None:
    """The knob, both audit actions and the join key all existed while no sweep did.

    important: this walks the AST for a CALL rather than grepping the source. The first version
    asserted the name appeared in `main`, which the `from ... import start_reconcile_sweep` line
    satisfies on its own, so replacing the call left the test green. That is the same shape as the
    defects this whole file exists to prevent, so it was worth fixing rather than loosening.
    """
    import ast
    import inspect

    from app import main

    tree = ast.parse(inspect.getsource(main))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "start_reconcile_sweep" in called, "nothing calls start_reconcile_sweep"

    # And it must be given the documented interval rather than a hardcoded one.
    source = inspect.getsource(main)
    assert "extension_reconcile_interval_seconds" in source, "the interval knob is not read"


@pytest.mark.parametrize("action", [
    "extension.install.scope_narrowed",
    "extension.credential.orphan_revoked",
])
def test_both_audit_actions_have_an_emitter(action: str) -> None:
    import inspect

    assert action in inspect.getsource(sweep), f"{action} is registered but nothing emits it"
