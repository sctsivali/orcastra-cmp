"""The reconcile sweep closes the drift window the request path leaves open.

`extension_scope` refuses a suspended or missing installation from a cached snapshot, and skips
the two-table join that decides whether the installing partner still reaches every cluster in the
grant; that drift is left to this sweep. Without it the stored grant would be trusted
indefinitely, and where clusters are rented one tenant's add-on could read the next tenant's
workload after the hardware changes hands.

A sweep that returns zero because it looked in the wrong place is indistinguishable from a clean
tree, so every case here pairs the change it expects with a case that must not change.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.core.api_key_capability_audit import capability_audit_fields
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
    single fake dispatching on the statement text would match the wrong branch."""

    installs: List[FakeInstall]
    bindings: Dict[int, Set[str]]
    commits: int = 0
    rollbacks: int = 0

    async def execute(self, _statement):
        # Returns every row. Filtering here would mirror a status filter in the real query and
        # hide it.
        return FakeResult(list(self.installs))

    async def commit(self):
        # Models `ck_ext_install_scope_nonempty`: the real database refuses an empty grant on
        # commit, and the suspension rolls back with it. A fake without the constraint cannot
        # test it.
        for install in self.installs:
            if install.granted_scope == []:
                raise IntegrityError(
                    "ck_ext_install_scope_nonempty", None, Exception("empty granted_scope")
                )
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@dataclass
class OrphanDB:
    installs: List[FakeInstall]
    commits: int = 0

    async def execute(self, _statement):
        return FakeResult([(i.api_key_id, i.previous_api_key_id) for i in self.installs])

    async def commit(self):
        self.commits += 1


@dataclass
class CredentialStore:
    """What the sweep did to the credential.

    The request path authorizes clusters from the Vault key, never from the row, so a narrowing
    that only writes the row changes nothing an add-on can feel.
    """

    rewritten: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    revoked: List[str] = field(default_factory=list)
    fail_on: Optional[str] = None

    def rewrite(self, _client, install) -> Dict[str, Any]:
        if self.fail_on == install.api_key_id:
            raise RuntimeError("vault is sealed")
        self.rewritten[install.api_key_id] = list(install.granted_scope or [])
        # What the real rewrite returns for a key whose capabilities it left unchanged.
        return capability_audit_fields({"capabilities": []}, {"capabilities": []})

    def revoke(self, _client, key_id: str) -> None:
        if self.fail_on == key_id:
            raise RuntimeError("vault is sealed")
        self.revoked.append(key_id)


def lock_from(installs: List[Any]):
    """sweep.lock_installation_id over a fake's rows: the row with that id, or 404 once it is gone."""
    async def _lock(_db, install_id: int):
        for install in installs:
            if install.id == install_id:
                return install
        raise HTTPException(status_code=404, detail="Installation not found")

    return _lock


def _run_scope_pass(db: ScopeDB, monkeypatch, store: Optional[CredentialStore] = None):
    store = store or CredentialStore()
    monkeypatch.setattr(sweep, "lock_installation_id", lock_from(db.installs))

    async def _bound(_db, organization_id: int):
        return db.bindings.get(organization_id, set())

    monkeypatch.setattr(sweep, "_bound_clusters", _bound)
    monkeypatch.setattr(sweep, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(sweep.activity_logger, "log_activity", lambda **_kw: None)
    monkeypatch.setattr(sweep, "_rewrite_key_grant", store.rewrite)
    monkeypatch.setattr(sweep, "revoke_install_credential", store.revoke)

    # ScopeDB answers every statement with installation rows, so the read of what a revoked
    # credential owns is answered here instead: nothing. test_an_add_on_credential_keeps_its_
    # organizations.py covers the record when it does own some.
    async def _owns_nothing(_db, _key_id):
        return []

    monkeypatch.setattr(sweep, "_owned_by", _owns_nothing)
    changed = asyncio.run(sweep._narrow_drifted_scope(db, object()))
    return changed, store


def test_a_grant_is_trimmed_to_what_the_organization_still_reaches(monkeypatch) -> None:
    install = FakeInstall(
        id=1, organization_id=3, api_key_id="k1",
        granted_scope=[{"cluster_id": "nuc2"}, {"cluster_id": "sold-on"}],
    )
    db = ScopeDB(installs=[install], bindings={3: {"nuc2"}})

    changed, store = _run_scope_pass(db, monkeypatch)
    assert changed == 1
    assert install.granted_scope == [{"cluster_id": "nuc2"}]
    assert install.status == "active", "it still reaches something, so it must not be suspended"
    # The request path reads the credential, so the credential must be narrowed as well as the
    # row.
    assert store.rewritten["k1"] == [{"cluster_id": "nuc2"}], (
        "the row was trimmed and the credential was not, so the add-on keeps the cluster"
    )


def test_an_installation_left_reaching_nothing_is_suspended(monkeypatch) -> None:
    install = FakeInstall(
        id=2, organization_id=3, api_key_id="k2", granted_scope=[{"cluster_id": "sold-on"}],
    )
    db = ScopeDB(installs=[install], bindings={3: set()})

    changed, store = _run_scope_pass(db, monkeypatch)
    assert changed == 1
    assert install.status == "suspended"
    assert "no longer reaches" in (install.suspend_reason or "")
    # The grant is not emptied. `ck_ext_install_scope_nonempty` refuses an empty one,
    # and the failed commit would roll the suspension back with it. The last grant stays as the
    # record of what was withdrawn, and the credential is revoked instead.
    assert install.granted_scope == [{"cluster_id": "sold-on"}]
    assert store.revoked == ["k2"], "reaching nothing must revoke the credential, not just the row"
    assert db.rollbacks == 0, "the constraint must never be hit in the first place"


def test_a_grant_that_still_fits_is_left_alone(monkeypatch) -> None:
    """The pass must be able to do nothing. Without this a sweep that rewrote every row would
    look identical to one that correctly found no drift."""
    install = FakeInstall(
        id=3, organization_id=3, api_key_id="k3", granted_scope=[{"cluster_id": "nuc2"}],
    )
    db = ScopeDB(installs=[install], bindings={3: {"nuc2", "pandora"}})

    changed, store = _run_scope_pass(db, monkeypatch)
    assert changed == 0
    assert db.commits == 0, "an unchanged grant must not write"
    assert install.granted_scope == [{"cluster_id": "nuc2"}]
    assert store.rewritten == {} and store.revoked == [], "nothing drifted, so nothing to rewrite"


def test_the_scope_pass_does_not_filter_by_status() -> None:
    """The scope pass reads suspended installations as well as active ones.

    ScopeDB returns whatever it holds without honouring a WHERE clause, so the behavioural cases
    below stay green if `status == "active"` is added to the real query. The filter is asserted
    here on the statement itself.
    """
    import inspect

    source = inspect.getsource(sweep._narrow_drifted_scope)
    select_body = source[source.index("select(ExtensionInstallation"):]
    select_body = select_body[: select_body.index(".scalars()")]
    assert 'status == "active"' not in select_body, (
        "the scope pass skips suspended installations again, so resuming one returns a grant its "
        "organization no longer has"
    )


def test_a_suspended_installation_is_narrowed_too(monkeypatch) -> None:
    """Resume only sets the status to active, so a suspended row skipped by the sweep would return
    holding a grant its organization does not have."""
    install = FakeInstall(
        id=4, organization_id=3, api_key_id="k4", status="suspended",
        granted_scope=[{"cluster_id": "nuc2"}, {"cluster_id": "sold-on"}],
    )
    db = ScopeDB(installs=[install], bindings={3: {"nuc2"}})

    changed, store = _run_scope_pass(db, monkeypatch)
    assert changed == 1
    assert install.granted_scope == [{"cluster_id": "nuc2"}]
    assert install.status == "suspended", "a suspended row must not be re-suspended"
    assert store.rewritten["k4"] == [{"cluster_id": "nuc2"}]


def test_a_suspended_row_keeps_the_reason_it_was_suspended_for(monkeypatch) -> None:
    """Narrowing must not overwrite why an operator suspended it."""
    install = FakeInstall(
        id=5, organization_id=3, api_key_id="k5", status="suspended",
        granted_scope=[{"cluster_id": "sold-on"}],
        suspend_reason="Operator: misbehaving",
    )
    db = ScopeDB(installs=[install], bindings={3: set()})

    changed, store = _run_scope_pass(db, monkeypatch)
    assert changed == 1
    assert install.suspend_reason == "Operator: misbehaving"
    assert store.revoked == ["k5"]


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
    """A key with no install marker belongs to the integrations surface rather than an add-on."""
    count, vault = _run_orphan_pass(
        installs=[],
        vault_keys=[{"id": "someone-elses-key", "name": "OrcaHub"}],
        monkeypatch=monkeypatch,
    )
    assert count == 0
    assert vault.revoked == []


def test_a_superseded_key_inside_its_grace_window_survives(monkeypatch) -> None:
    """Rotation retires the superseded key by shortening its expiry, so it is still usable and is
    not an orphan. Revoking it here would break the overlap rotation exists to provide."""
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
    """`main` calls start_reconcile_sweep with the configured interval.

    This walks the AST for a call rather than grepping the source: the
    `from ... import start_reconcile_sweep` line alone would satisfy a name check.
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

    # It must be given the configured interval rather than a hardcoded one.
    source = inspect.getsource(main)
    assert "extension_reconcile_interval_seconds" in source, "the interval knob is not read"


@pytest.mark.parametrize("action", [
    "extension.install.scope_narrowed",
    "extension.credential.orphan_revoked",
])
def test_both_audit_actions_have_an_emitter(action: str) -> None:
    import inspect

    assert action in inspect.getsource(sweep), f"{action} is registered but nothing emits it"


def test_a_sealed_vault_delays_one_row_rather_than_ending_the_pass(monkeypatch) -> None:
    """One failing row rolls back on its own and does not end the scope pass.

    Here the first row cannot reach Vault and the second still gets its narrowing.
    """
    stuck = FakeInstall(
        id=6, organization_id=3, api_key_id="stuck",
        granted_scope=[{"cluster_id": "nuc2"}, {"cluster_id": "sold-on"}],
    )
    fine = FakeInstall(
        id=7, organization_id=3, api_key_id="fine",
        granted_scope=[{"cluster_id": "nuc2"}, {"cluster_id": "sold-on"}],
    )
    db = ScopeDB(installs=[stuck, fine], bindings={3: {"nuc2"}})

    changed, store = _run_scope_pass(db, monkeypatch, CredentialStore(fail_on="stuck"))

    assert changed == 1, "the healthy row must still be narrowed"
    assert store.rewritten["fine"] == [{"cluster_id": "nuc2"}]
    assert "stuck" not in store.rewritten
    assert db.rollbacks == 1, "the failed row must roll back rather than leave a half-write"


def test_the_scope_pass_rewrites_the_credential_not_only_the_row() -> None:
    """A structural backstop to the cases above: the pass rewrites the credential and never writes
    an empty grant. The request path reads `cluster_permissions` from the key."""
    import inspect

    source = inspect.getsource(sweep._narrow_drifted_scope)
    assert "_rewrite_key_grant" in source, (
        "the sweep trims the row without rewriting the credential, which changes nothing a "
        "request can feel"
    )
    assert "granted_scope = []" not in source, (
        "an empty grant violates ck_ext_install_scope_nonempty and takes the suspension down "
        "with it"
    )
