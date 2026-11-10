"""An installation must never be able to mint a credential that grants everything.

`granted_capabilities` returns None for a key whose `capabilities` field is absent, and
`has_capability` reads None as unrestricted, which keeps keys issued before capabilities existed
working. A code path that forgets to write the field therefore mints a credential with no ceiling,
and nothing about the key looks wrong. `build_install_metadata` is the only writer and spells the
key literally; this file pins that structurally and checks the result through
`granted_capabilities` itself.

The credential is also hash-only: the plaintext backfill in `_validate_api_key_blocking` is guarded
on `app_url`, and an installation never has one, so nothing writes a recoverable copy.
"""

import ast
import inspect
from datetime import timedelta
from typing import Any, Dict

import pytest

from app.core.api_key_capabilities import granted_capabilities, has_capability
from app.core.api_key_expiry import is_expired, now_utc, parse_expiry
from app.services import extension_credentials as ec


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


@pytest.fixture(autouse=True)
def _hmac_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")


@pytest.fixture
def issued(monkeypatch):
    monkeypatch.setattr(ec, "evict_api_key_cache", lambda _key_id: None)
    vault = FakeVault()
    key_id, secret, signing, metadata = ec.issue_install_credential(
        vault,
        extension_slug="acme-backup",
        extension_name="Acme Backup",
        install_id=7,
        organization_id=3,
        organization_slug="acme",
        capabilities=["lxd.proxy", "inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc2", "permissions": ["read"]}],
        created_by="ops@example.com",
        expire_days=365,
    )
    return vault, key_id, secret, signing, metadata


# ── the behavioural half ───────────────────────────────────────────────────────────────────────

def test_an_issued_credential_is_not_unrestricted(issued) -> None:
    """Checked through `granted_capabilities`, whose None return means unrestricted."""
    _vault, _key_id, _secret, _signing, metadata = issued

    granted = granted_capabilities(metadata)
    assert granted is not None, "an issued credential read back as legacy, which grants everything"
    assert granted == {"inventory.read", "lxd.proxy"}
    assert not has_capability(metadata, "instance.exec")
    assert not has_capability(metadata, "org.sync")


def test_an_issued_credential_leaves_no_recoverable_secret(issued) -> None:
    vault, key_id, secret, _signing, _metadata = issued
    payload = vault.store[f"secret/data/integrations/api_keys/{key_id}/secret"]
    assert set(payload) == {"hash"}, f"unexpected fields beside the hash: {sorted(payload)}"
    assert secret not in repr(payload)


def test_the_signing_secret_is_not_the_credential(issued) -> None:
    """Signing with the API secret would hand a live key to anyone who can verify a delivery, so
    the two secrets are split and stored on different paths."""
    vault, key_id, secret, signing, _metadata = issued
    assert signing != secret
    assert signing.startswith("whsec_")
    assert f"secret/data/extensions/installations/7/webhook" in vault.store
    assert secret not in repr(vault.store)


def test_rotation_keeps_the_ceiling(issued, monkeypatch) -> None:
    vault, old_key_id, _secret, _signing, _metadata = issued
    new_key_id, new_secret, new_metadata = ec.rotate_install_credential(
        vault,
        old_key_id=old_key_id,
        extension_slug="acme-backup",
        extension_name="Acme Backup",
        install_id=7,
        organization_id=3,
        organization_slug="acme",
        capabilities=["inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc2", "permissions": ["read"]}],
        created_by="ops@example.com",
        expire_days=365,
        credential_generation=1,
        grace_seconds=3600,
    )
    assert new_key_id != old_key_id, "rotation reused the key id, so there is no overlap window"
    assert granted_capabilities(new_metadata) == {"inventory.read"}
    assert new_metadata["credential_generation"] == 2
    assert new_metadata["supersedes"] == old_key_id


def test_rotation_retires_the_old_key_through_its_expiry(issued, monkeypatch) -> None:
    """`validate_api_key` already refuses an expired key with 401, so shortening the expiry is the
    whole mechanism; there is no separate "retiring" flag to enforce."""
    vault, old_key_id, _secret, _signing, _metadata = issued
    ec.rotate_install_credential(
        vault,
        old_key_id=old_key_id,
        extension_slug="acme-backup",
        extension_name="Acme Backup",
        install_id=7,
        organization_id=3,
        organization_slug="acme",
        capabilities=["inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc2", "permissions": ["read"]}],
        created_by="ops@example.com",
        expire_days=365,
        credential_generation=1,
        grace_seconds=600,
    )
    retired = vault.store[f"secret/data/integrations/api_keys/{old_key_id}/metadata"]
    expiry = parse_expiry(retired["expires_at"])
    assert expiry is not None
    # Alive now, dead shortly after the window.
    assert not is_expired(retired["expires_at"])
    assert is_expired(retired["expires_at"], now=now_utc() + timedelta(seconds=601))


def test_rotation_with_no_grace_revokes_outright(issued) -> None:
    vault, old_key_id, _secret, _signing, _metadata = issued
    ec.rotate_install_credential(
        vault,
        old_key_id=old_key_id,
        extension_slug="acme-backup",
        extension_name="Acme Backup",
        install_id=7,
        organization_id=3,
        organization_slug="acme",
        capabilities=["inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc2", "permissions": ["read"]}],
        created_by="ops@example.com",
        expire_days=365,
        credential_generation=1,
        grace_seconds=0,
    )
    retired = vault.store[f"secret/data/integrations/api_keys/{old_key_id}/metadata"]
    assert retired["is_revoked"] is True
    assert retired["is_active"] is False


def test_issuing_without_a_capability_is_refused(issued) -> None:
    vault, *_ = issued
    with pytest.raises(ValueError):
        ec.issue_install_credential(
            vault,
            extension_slug="x", extension_name="x", install_id=8, organization_id=1,
            organization_slug=None, capabilities=[], cluster_permissions=[],
            created_by="ops@example.com", expire_days=1,
        )


# ── the structural half ────────────────────────────────────────────────────────────────────────

def test_only_one_function_builds_installation_key_metadata() -> None:
    """A second builder could leave the field out on its path unnoticed.

    A dict literal carrying `app_type` and `cluster_permissions` is an API key metadata blob, so
    one outside `build_install_metadata` is a second writer.
    """
    source = inspect.getsource(ec)
    tree = ast.parse(source)

    builders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        if {"app_type", "cluster_permissions"} <= keys:
            builders.append(keys)

    assert len(builders) == 1, (
        f"{len(builders)} places build installation key metadata; there must be exactly one"
    )
    assert "capabilities" in builders[0], (
        "the one builder does not spell `capabilities`, so an issued key would read as legacy "
        "and grant everything"
    )


def test_the_builder_never_merges_a_caller_supplied_blob() -> None:
    """`**overrides` would let a caller drop the key the literal exists to guarantee."""
    source = inspect.getsource(ec.build_install_metadata)
    assert "**" not in source.split("metadata: Dict[str, Any] = {")[1].split("}")[0]
    signature = inspect.signature(ec.build_install_metadata)
    assert not any(
        p.kind is p.VAR_KEYWORD for p in signature.parameters.values()
    ), "build_install_metadata accepts **kwargs, so a caller can shape the metadata blob"


def test_an_installation_credential_never_asks_for_a_recoverable_secret() -> None:
    """`raw_secret=` must never be passed anything but None on this path."""
    tree = ast.parse(inspect.getsource(ec))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "save_api_key_to_vault"):
            continue
        raw = [kw for kw in node.keywords if kw.arg == "raw_secret"]
        assert raw, "save_api_key_to_vault called without stating raw_secret explicitly"
        assert isinstance(raw[0].value, ast.Constant) and raw[0].value.value is None, (
            "an installation credential asked for a recoverable copy of its secret"
        )
