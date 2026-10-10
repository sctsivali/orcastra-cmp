"""Every write of an add-on credential's capability set records what it granted and withdrew.

Installing, rescoping and rotating an add-on, and the sweep that narrows one, all write the
capability set of an integration key. Their records carried the new set only. They now carry the
same fields as an integration key edit: the set before, what was granted and withdrawn, and the
elevated capabilities among them, built by core/api_key_capability_audit. The existing action
names and fields are unchanged.

The set before a rewrite is the key's own, read from Vault, not the installation row's. A key can
be edited through the integration capabilities route, so the two can differ, and the rewrite then
withdraws what that edit added.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization
from app.models.extension_models import Extension, ExtensionInstallation
from app.routers import extension_installs, integrations
from app.services import extension_credentials, extension_install, extension_reconcile
from app.services import extension_scope, extension_service
from app.services.webhooks import emitter
from tests.test_a_capability_grant_leaves_a_record import _FakeVault, _RecordingLogger

PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])
DIGEST = "a" * 64
REQUESTED = ["inventory.read", "lxd.proxy", "org.sync", "tenant.provision"]
KEY = "oak_addon"


def _extension() -> Extension:
    return Extension(
        id=3, slug="billing", name="Billing", version="1.0.0", status="published",
        requested_capabilities=list(REQUESTED), requested_scope={}, manifest={"name": "Billing"},
        settings_schema=None,
    )


def _install(capabilities: List[str], api_key_id: str = KEY) -> ExtensionInstallation:
    return ExtensionInstallation(
        id=11, extension_id=3, organization_id=5, status="active",
        granted_capabilities=list(capabilities),
        granted_scope=[{"cluster_id": "nuc1", "permissions": ["read"], "projects": ["default"]}],
        api_key_id=api_key_id, credential_generation=1, consent_version="1.0.0",
        consent_manifest_digest=DIGEST, installed_by=PARTNER.email,
        updated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def _grant(cluster: str = "nuc1") -> Dict[str, Any]:
    return {"cluster_id": cluster, "permissions": ["read"], "projects": ["default"]}


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value


class _Rows:
    def __init__(self, rows: List[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> List[Any]:
        return self.rows


class _DB:
    """Answers the install path's lookups: no existing installation, none counted, one organization.

    A rotation also reads which organizations the old credential owns, to lock their owner sets, and
    then reads them FOR UPDATE: none here.
    """

    def __init__(self) -> None:
        self.added: List[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> Any:
        text = str(statement)
        if "FOR UPDATE" in text or "FROM organization_owner_keys" in text:
            return _Rows([])
        if "count(" in text:
            return _Result(0)
        if "FROM organizations" in text:
            return _Result(Organization(id=5, slug="acme", name="Acme"))
        return _Result(None)

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        for instance in self.added:
            if getattr(instance, "id", None) is None:
                instance.id = 11

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _instance: Any) -> None:
        return None


@pytest.fixture
def surface(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {"log": _RecordingLogger(), "events": []}
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    monkeypatch.setattr(extension_installs, "activity_logger", holder["log"])
    monkeypatch.setattr(extension_service, "manifest_matches", lambda _e, _d: True)
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    monkeypatch.setattr(extension_install, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(extension_credentials, "evict_api_key_cache", lambda _k: None)
    monkeypatch.setattr(extension_install, "to_response", lambda record, *_a, **_k: record)
    monkeypatch.setattr(extension_installs, "InstallCreateResponse", lambda **kw: SimpleNamespace(**kw))

    async def _within(*_a: Any, **_k: Any) -> None:
        return None

    async def _event(_db: Any, *, event_type: str, installation: Any) -> None:
        holder["events"].append(event_type)

    async def _found(*_a: Any, **_k: Any) -> Extension:
        return _extension()

    monkeypatch.setattr(extension_install, "assert_install_scope_within_partner", _within)
    monkeypatch.setattr(extension_scope, "assert_install_scope_within_partner", _within)
    monkeypatch.setattr(emitter, "emit_installation_event", _event)
    monkeypatch.setattr(extension_service, "get_extension_or_404", _found)

    def use(vault: _FakeVault) -> _FakeVault:
        monkeypatch.setattr(extension_installs, "get_vault_integrations_client", lambda: vault)
        holder["vault"] = vault
        return vault

    def load(record: ExtensionInstallation) -> None:
        async def _load(_db: Any, _install_id: int, _user: Any) -> tuple:
            return record, _extension(), "acme"

        async def _locked(_db: Any, install: ExtensionInstallation) -> ExtensionInstallation:
            return install

        monkeypatch.setattr(extension_installs, "_load", _load)
        # note: the row lock re-reads the same object in production; this fake database has no rows
        monkeypatch.setattr(extension_install, "lock_installation", _locked)

    holder["use"] = use
    holder["load"] = load
    return holder


def _metadata(vault: _FakeVault, key_id: str) -> Dict[str, Any]:
    return vault.store[_FakeVault._path(key_id, "metadata")]


def _no_secret_in(call: Dict[str, Any], *secrets: Optional[str]) -> None:
    text = repr(call)
    for secret in secrets:
        assert secret and secret not in text


# ── install ─────────────────────────────────────────────────────────────────────────────────────

async def test_an_install_records_the_whole_set_as_granted(surface: Dict[str, Any]) -> None:
    vault = surface["use"](_FakeVault({}))
    request = SimpleNamespace(
        organization_id=5, consent=SimpleNamespace(version="1.0.0", manifest_digest=DIGEST),
        capabilities=["inventory.read", "org.sync"], cluster_permissions=[_grant()], settings=None,
    )
    created = await extension_installs.install("billing", request, None, PARTNER, _DB())

    written = _metadata(vault, created.api_key_id)
    assert written["capabilities"] == ["inventory.read", "org.sync"]
    (call,) = surface["log"].calls
    assert call["action"] == "extension.install"
    details = call["details"]
    assert details["extension_slug"] == "billing" and details["api_key_id"] == created.api_key_id
    assert details["capabilities"] == written["capabilities"]
    assert details["cluster_ids"] == ["nuc1"]
    assert details["capabilities_before"] == []
    assert details["capabilities_granted"] == written["capabilities"]
    assert details["capabilities_withdrawn"] == []
    assert details["elevated_capabilities"] == ["org.sync"]
    assert details["elevated_capabilities_granted"] == ["org.sync"]
    _no_secret_in(call, created.api_key_secret, created.webhook_signing_secret)


# ── rescope ─────────────────────────────────────────────────────────────────────────────────────

async def test_a_rescope_records_the_keys_own_set_before_it(surface: Dict[str, Any]) -> None:
    """The key was widened to org.sync through the integration route, and the row never saw it."""
    vault = surface["use"](_FakeVault({
        "id": KEY, "capabilities": ["inventory.read", "org.sync"], "cluster_permissions": [_grant()],
        "extension_install_id": 11, "app_url": "https://callback.example.test",
    }))
    record = _install(["inventory.read"])
    surface["load"](record)
    request = SimpleNamespace(
        consent=SimpleNamespace(version="1.0.0", manifest_digest=DIGEST),
        capabilities=["inventory.read", "lxd.proxy"], cluster_permissions=[_grant()],
    )
    await extension_installs.rescope(11, request, None, PARTNER, _DB())

    assert _metadata(vault, KEY)["capabilities"] == ["inventory.read", "lxd.proxy"]
    (call,) = surface["log"].calls
    assert call["action"] == "extension.install.rescope"
    details = call["details"]
    assert details["capabilities"] == ["inventory.read", "lxd.proxy"]
    assert details["cluster_ids"] == ["nuc1"]
    assert details["api_key_id"] == KEY
    assert details["capabilities_before"] == ["inventory.read", "org.sync"]
    assert details["capabilities_granted"] == ["lxd.proxy"]
    assert details["capabilities_withdrawn"] == ["org.sync"]
    assert details["elevated_capabilities"] == []
    assert details["elevated_capabilities_granted"] == []
    _no_secret_in(call, "https://callback.example.test", "hash-do-not-log", "oas_do_not_log_me")


def test_the_rewrite_reports_the_key_it_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    vault = _FakeVault({"id": KEY, "capabilities": ["inventory.read"], "cluster_permissions": []})
    fields = extension_install._rewrite_key_grant(vault, _install(["inventory.read", "tenant.provision"]))
    assert fields["capabilities_before"] == ["inventory.read"]
    assert fields["capabilities_granted"] == ["tenant.provision"]
    assert fields["elevated_capabilities_granted"] == ["tenant.provision"]
    assert _metadata(vault, KEY)["capabilities"] == ["inventory.read", "tenant.provision"]


# ── rotate ──────────────────────────────────────────────────────────────────────────────────────

async def test_a_rotation_records_the_new_credential_as_a_grant(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = surface["use"](_FakeVault({
        "id": KEY, "capabilities": ["inventory.read", "tenant.provision"], "cluster_permissions": [_grant()],
    }))
    record = _install(["inventory.read", "tenant.provision"])
    surface["load"](record)
    monkeypatch.setattr(
        extension_install, "get_settings",
        lambda: SimpleNamespace(
            extension_credential_min_rotate_seconds=0, extension_credential_max_grace_seconds=0,
            extension_credential_expire_days=30,
        ),
    )
    created = await extension_installs.rotate(11, 0, None, PARTNER, _DB())

    assert created.api_key_id != KEY
    assert _metadata(vault, created.api_key_id)["capabilities"] == ["inventory.read", "tenant.provision"]
    (call,) = surface["log"].calls
    assert call["action"] == "extension.install.credential.rotate"
    details = call["details"]
    assert details["api_key_id"] == created.api_key_id and details["grace_seconds"] == 0
    assert details["capabilities"] == ["inventory.read", "tenant.provision"]
    assert details["capabilities_before"] == []
    assert details["capabilities_granted"] == ["inventory.read", "tenant.provision"]
    assert details["elevated_capabilities_granted"] == ["tenant.provision"]
    _no_secret_in(call, created.api_key_secret, created.webhook_signing_secret)


# ── the sweep ───────────────────────────────────────────────────────────────────────────────────

class _ScopeDB:
    def __init__(self, installs: List[ExtensionInstallation]) -> None:
        self.installs = installs

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.installs)))

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:  # pragma: no cover - a failed narrowing is another test's
        raise AssertionError("the narrowing failed")


def test_a_narrowing_records_the_capability_change(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _RecordingLogger()
    monkeypatch.setattr(extension_reconcile, "activity_logger", log)
    monkeypatch.setattr(extension_reconcile, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)

    async def _bound(_db: Any, _org: int) -> set:
        return {"nuc1"}

    monkeypatch.setattr(extension_reconcile, "_bound_clusters", _bound)
    vault = _FakeVault({
        "id": KEY, "capabilities": ["inventory.read", "org.sync"],
        "cluster_permissions": [_grant(), _grant("sold-on")],
    })
    install = _install(["inventory.read"])
    install.granted_scope = [_grant(), _grant("sold-on")]

    changed = asyncio.run(extension_reconcile._narrow_drifted_scope(_ScopeDB([install]), vault))

    assert changed == 1
    assert _metadata(vault, KEY)["cluster_permissions"] == [_grant()]
    (call,) = log.calls
    assert call["action"] == "extension.install.scope_narrowed"
    details = call["details"]
    assert details["dropped_clusters"] == ["sold-on"] and details["remaining"] == 1
    assert details["api_key_id"] == KEY
    assert details["capabilities_before"] == ["inventory.read", "org.sync"]
    assert details["capabilities_withdrawn"] == ["org.sync"]
    assert details["capabilities_granted"] == []
