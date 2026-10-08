"""Every capability grant and every edit of one leaves an audit record.

A Partner may issue org.sync and tenant.provision. Their effect is capped where it lands (an
integration grants a role, never demotes one), and the grant itself has to be answerable: who
handed an unattended caller which power, and when. Creation was already recorded. Editing a key's
capability set, which can widen a read key into one that writes organizations, left nothing but an
application log line.

The record is built from capability names only, so a secret, its hash or app_url in the key's
metadata never reaches it.
"""

import ast
import collections
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.core.api_key_capabilities import CAPABILITIES, GUEST_ACCESS_CAPABILITIES
from app.core.api_key_capability_audit import CAPABILITIES_UPDATE_ACTION, capability_audit_fields
from app.core.auth import AuthenticatedUser
from app.routers import integrations
from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS, EventCategory
from tests._sqlite_session import org_tables, sqlite_session

SECRETS = ("oas_do_not_log_me", "hash-do-not-log", "https://callback.example.test")
PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])


def test_the_edit_action_is_registered_and_high_severity() -> None:
    assert CAPABILITIES_UPDATE_ACTION == "integration.api_key.capabilities.update"
    assert ACTION_CATEGORIES.get(CAPABILITIES_UPDATE_ACTION) is EventCategory.ADMIN
    assert CAPABILITIES_UPDATE_ACTION in HIGH_SEVERITY_ACTIONS


# ── the fields ─────────────────────────────────────────────────────────────────────────────────

def test_a_grant_names_its_elevated_capabilities() -> None:
    fields = capability_audit_fields({"capabilities": ["inventory.read", "org.sync", "tenant.provision"]})
    assert fields == {"elevated_capabilities": ["org.sync", "tenant.provision"]}


def test_an_edit_says_what_it_granted_and_withdrew() -> None:
    fields = capability_audit_fields(
        {"capabilities": ["inventory.read", "org.sync"]},
        {"capabilities": ["inventory.read", "lxd.proxy"]},
    )
    assert fields["capabilities_before"] == ["inventory.read", "lxd.proxy"]
    assert fields["capabilities_granted"] == ["org.sync"]
    assert fields["capabilities_withdrawn"] == ["lxd.proxy"]
    assert fields["elevated_capabilities_granted"] == ["org.sync"]


def test_narrowing_a_legacy_key_withdraws_what_it_could_use() -> None:
    fields = capability_audit_fields({"capabilities": ["inventory.read"]}, {"id": "oak_legacy"})
    assert fields["capabilities_before"] is None
    assert fields["capabilities_granted"] == []
    assert fields["capabilities_withdrawn"] == sorted(
        set(CAPABILITIES) - GUEST_ACCESS_CAPABILITIES - {"inventory.read"}
    )


def test_guest_access_on_a_legacy_key_is_a_grant() -> None:
    """A legacy key never inherited instance.exec, so adding it is a grant, not a no-op."""
    fields = capability_audit_fields({"capabilities": ["instance.exec"]}, {})
    assert fields["capabilities_granted"] == ["instance.exec"]
    assert fields["elevated_capabilities_granted"] == ["instance.exec"]


def test_only_vocabulary_names_reach_the_record() -> None:
    fields = capability_audit_fields(
        {"capabilities": ["org.sync", SECRETS[0]], "secret": SECRETS[1], "app_url": SECRETS[2]},
        {"capabilities": [SECRETS[1]], "hash": SECRETS[1]},
    )
    assert not any(secret in repr(fields) for secret in SECRETS)


# ── the routes ─────────────────────────────────────────────────────────────────────────────────

class _RecordingLogger:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "evt_test"


class _FakeVault:
    """Key metadata and secret as Vault holds them, with every write recorded and applied."""

    def __init__(self, metadata: Dict[str, Any]) -> None:
        self.store: Dict[str, Dict[str, Any]] = {}
        if metadata:
            key = metadata["id"]
            self.store[self._path(key, "metadata")] = dict(metadata)
            self.store[self._path(key, "secret")] = {"hash": SECRETS[1], "raw": SECRETS[0]}
        self.writes: List[Tuple[str, Dict[str, Any]]] = []

    @staticmethod
    def _path(key: str, leaf: str) -> str:
        return f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{key}/{leaf}"

    def vault_read(self, path: str) -> Dict[str, Any]:
        return dict(self.store[path])

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        self.writes.append((path, dict(data)))
        self.store[path] = dict(data)


class _NoCache:
    def delete_pattern(self, *_a: Any) -> None:
        return None

    def delete(self, *_a: Any) -> None:
        return None


@pytest.fixture
def surface(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {"log": _RecordingLogger()}
    monkeypatch.setattr(integrations, "activity_logger", holder["log"])
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _NoCache())
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda *_a: [])

    def use(metadata: Dict[str, Any]) -> _FakeVault:
        vault = _FakeVault(metadata)
        monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
        holder["vault"] = vault
        return vault

    holder["use"] = use
    return holder


def _key(**extra: Any) -> Dict[str, Any]:
    return {
        "id": "oak_edit", "name": "k", "app_name": "OrcaHub", "app_type": "orcahub",
        "created_by": "partner@example.test", "created_at": "2026-09-01T00:00:00+00:00",
        "expires_at": None, "is_active": True, "is_revoked": False, "last_used_at": None,
        "cluster_permissions": [{"cluster_id": "nuc1", "permissions": ["read"]}],
        "app_url": SECRETS[2], **extra,
    }


async def test_widening_a_key_to_org_sync_is_recorded(surface: Dict[str, Any]) -> None:
    vault = surface["use"](_key(capabilities=["inventory.read"]))
    await integrations.update_api_key_capabilities(
        "oak_edit",
        integrations.APIKeyCapabilitiesRequest(capabilities=["inventory.read", "org.sync"]),
        None,
        PARTNER,
        None,
    )
    assert vault.store[_FakeVault._path("oak_edit", "metadata")]["capabilities"] == [
        "inventory.read", "org.sync",
    ]
    calls = surface["log"].calls
    assert [c["action"] for c in calls] == [CAPABILITIES_UPDATE_ACTION]
    call = calls[0]
    assert call["target_id"] == "oak_edit" and call["target_type"] == "api_key"
    details = call["details"]
    assert details["capabilities"] == ["inventory.read", "org.sync"]
    assert details["capabilities_before"] == ["inventory.read"]
    assert details["capabilities_granted"] == ["org.sync"]
    assert details["elevated_capabilities_granted"] == ["org.sync"]
    assert details["cluster_ids"] == ["nuc1"]
    assert not any(secret in repr(call) for secret in SECRETS)


async def test_narrowing_a_key_is_recorded_too(surface: Dict[str, Any]) -> None:
    surface["use"](_key(capabilities=["inventory.read", "tenant.provision"]))
    await integrations.update_api_key_capabilities(
        "oak_edit",
        integrations.APIKeyCapabilitiesRequest(capabilities=["inventory.read"]),
        None,
        PARTNER,
        None,
    )
    (call,) = surface["log"].calls
    assert call["details"]["capabilities_withdrawn"] == ["tenant.provision"]
    assert call["details"]["elevated_capabilities_granted"] == []


async def test_a_failed_audit_write_does_not_fail_the_edit(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Exploding:
        def log_activity(self, **_: Any) -> str:
            raise RuntimeError("audit sink is down")

    monkeypatch.setattr(integrations, "activity_logger", _Exploding())
    vault = surface["use"](_key(capabilities=["inventory.read"]))
    # note: a database, because the edit withdraws inventory.read and so first reads which
    # organizations the key owns. It owns none.
    info = await integrations.update_api_key_capabilities(
        "oak_edit",
        integrations.APIKeyCapabilitiesRequest(capabilities=["org.sync"]),
        None,
        PARTNER,
        sqlite_session(*org_tables()),
    )
    assert info.capabilities == ["org.sync"]
    assert vault.writes


async def test_a_partner_minting_an_orcahub_key_records_the_elevated_grant(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _nothing)
    monkeypatch.setattr(integrations, "_assert_granted_projects_exist", _nothing)
    vault = surface["use"]({})
    created = await integrations.create_api_key(
        integrations.APIKeyCreateRequest(name="k", app_name="OrcaHub", app_type="orcahub"),
        None, PARTNER, None, None,
    )
    assert any(path.endswith(f"/{created.api_key_id}/metadata") for path, _ in vault.writes)
    (call,) = surface["log"].calls
    assert call["action"] == "integration.api_key.create"
    assert call["details"]["elevated_capabilities"] == ["org.sync", "tenant.provision"]
    assert created.api_key_secret not in repr(call)


# ── the ratchet ────────────────────────────────────────────────────────────────────────────────
#
# Every function under app/ that writes a key's capability set is found by shape, and every call
# chain above it must end at a function that writes an audit record, with the capability fields
# built somewhere on that chain. The behavioural tests (this file for integration keys,
# test_an_add_on_credential_grant_leaves_a_record for add-on credentials) prove the fields land.

APP = Path(__file__).resolve().parents[1] / "app"
AUDIT_SINKS = {"log_activity", "_audit", "_audit_credential_lifecycle"}
FIELD_BUILDERS = {"capability_audit_fields", "issued_credential_audit_fields", "_audit_credential_lifecycle"}

KEY_WRITERS = {
    ("app.routers.integrations", "create_api_key"),
    ("app.routers.integrations", "update_api_key_capabilities"),
    ("app.services.extension_credentials", "build_install_metadata"),
    ("app.services.extension_install", "_rewrite_key_grant"),
}
# Each recording point a key writer reaches, and the action its record carries.
RECORDS = {
    ("app.routers.integrations", "create_api_key"): "integration.api_key.create",
    ("app.routers.integrations", "update_api_key_capabilities"): CAPABILITIES_UPDATE_ACTION,
    ("app.routers.extension_installs", "install"): "extension.install",
    ("app.routers.extension_installs", "rescope"): "extension.install.rescope",
    ("app.routers.extension_installs", "rotate"): "extension.install.credential.rotate",
    ("app.services.extension_reconcile", "_narrow_drifted_scope"): "extension.install.scope_narrowed",
}
# A "capabilities" field that is not an integration key's grant.
NOT_A_KEY = {
    ("app.routers.extensions", "get_vocabulary"): "the capability vocabulary an add-on author reads",
    ("app.routers.extensions", "register_extension"): "an add-on manifest's requested capabilities",
    ("app.routers.extensions", "publish_extension_version"): "an add-on manifest's requested capabilities",
    ("app.services.extension_install", "build_install_preview"): "the consent preview shown before install",
    ("app.services.instance_specifications", "read_specification_sync"): "an LXD instance's capabilities",
    ("app.services.migrations.plan_contracts", "_preflight_payload"): "a migration plan's capability snapshot",
    ("app.services.migrations.plan_contracts", "approval_report_hash"): "a migration plan's capability snapshot",
    ("app.services.migrations.planning", "_preflight"): "a migration plan's capability snapshot",
}


def _callee(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _exempt_dicts(fn: ast.AST) -> set:
    """A dict handed to an audit record or to a field builder describes a grant, it is not one."""
    exempt = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.keyword) and node.arg == "details":
            exempt.add(id(node.value))
        if isinstance(node, ast.Call) and _callee(node) in FIELD_BUILDERS:
            exempt.update(id(arg) for arg in node.args)
    return exempt


def _writes_capabilities(fn: ast.AST) -> bool:
    exempt = _exempt_dicts(fn)
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if isinstance(node.slice, ast.Constant) and node.slice.value == "capabilities":
                return True
        if isinstance(node, ast.Dict) and id(node) not in exempt and any(
            isinstance(k, ast.Constant) and k.value == "capabilities" for k in node.keys
        ):
            return True
        if isinstance(node, ast.Call) and _callee(node) in {"dict", "update", "setdefault"} and any(
            kw.arg == "capabilities" for kw in node.keywords
        ):
            return True
    return False


def _index() -> Tuple[Dict[Tuple[str, str], ast.AST], Dict[str, set]]:
    functions: Dict[Tuple[str, str], ast.AST] = {}
    callers: Dict[str, set] = collections.defaultdict(set)
    for path in sorted(APP.rglob("*.py")):
        module = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[(module, fn.name)] = fn
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and _callee(node):
                        callers[_callee(node)].add((module, fn.name))
    return functions, callers


def _calls(fn: ast.AST) -> set:
    return {_callee(node) for node in ast.walk(fn) if isinstance(node, ast.Call)}


def _chains(writer: Tuple[str, str], functions: Dict, callers: Dict) -> List[List[Tuple[str, str]]]:
    """Every call chain from the writer up to a function that records, or to a root that does not."""
    chains, stack = [], [[writer]]
    while stack:
        chain = stack.pop()
        top = chain[-1]
        above = [c for c in sorted(callers.get(top[1], ())) if c not in chain]
        if _calls(functions[top]) & AUDIT_SINKS or not above:
            chains.append(chain)
            continue
        stack.extend(chain + [c] for c in above)
    return chains


def test_every_writer_of_a_key_capability_set_is_known() -> None:
    functions, _ = _index()
    writers = {name for name, fn in functions.items() if _writes_capabilities(fn)}
    assert writers - set(NOT_A_KEY) == KEY_WRITERS, (
        "a new function writes a 'capabilities' field: if it is a key's grant add it to KEY_WRITERS "
        "and record it, otherwise add it to NOT_A_KEY with the reason"
    )
    assert set(NOT_A_KEY) <= writers, f"stale NOT_A_KEY entries: {sorted(set(NOT_A_KEY) - writers)}"


def test_every_key_capability_write_reaches_a_record_with_the_fields() -> None:
    functions, callers = _index()
    reached = set()
    for writer in sorted(KEY_WRITERS):
        for chain in _chains(writer, functions, callers):
            point = chain[-1]
            assert _calls(functions[point]) & AUDIT_SINKS, (
                f"{' <- '.join(f'{m}.{n}' for m, n in chain)} writes a key's capabilities with no audit record"
            )
            assert any(_calls(functions[link]) & FIELD_BUILDERS for link in chain), (
                f"{' <- '.join(n for _m, n in chain)} records without the capability audit fields"
            )
            reached.add(point)
    assert reached == set(RECORDS)
    for point, action in RECORDS.items():
        source = ast.unparse(functions[point])
        named = f"'{action}'" in source or (
            action == CAPABILITIES_UPDATE_ACTION and "CAPABILITIES_UPDATE_ACTION" in source
        )
        assert named, f"{point[0]}.{point[1]} does not write {action}"


def test_the_lifecycle_record_builds_the_capability_fields() -> None:
    functions, _ = _index()
    assert "capability_audit_fields" in _calls(functions[("app.routers.integrations", "_audit_credential_lifecycle")])
