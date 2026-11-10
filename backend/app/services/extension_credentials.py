"""Issuing, rotating and retiring the credential that belongs to one installation.

Vault operations reuse the integration key machinery, so an installation credential authenticates
through the same path as any other key and shares its cache, cross-worker revocation and per-key
rate limit.

It differs from an ordinary key in two ways. It is hash-only: `save_api_key_to_vault` is called
with `raw_secret=None`, and the backfill in `_validate_api_key_blocking` runs only for keys with
`app_url`, which an installation credential never has. And deliveries are signed with a separate
secret. The OrcaHub emitter signs with the API key's own secret, so anyone able to verify a
signature holds a live credential; here one secret authenticates the add-on, the other signs
deliveries, and rotating either leaves the other alone.
"""

import logging
import secrets
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.core.api_key_expiry import now_utc, utc_isoformat
from app.routers.integrations import (
    VAULT_INTEGRATIONS_PATH,
    delete_api_key_from_vault,
    evict_api_key_cache,
    generate_api_key_pair,
    get_api_key_from_vault,
    save_api_key_to_vault,
)

logger = logging.getLogger(__name__)

# The signing secret lives beside the credential but on its own path, so a reader granted one is
# not automatically granted the other.
VAULT_EXTENSIONS_PATH = "extensions"

SIGNING_SECRET_PREFIX = "whsec_"

# `automation` already exists in APP_TYPES and its defaults are the non-elevated capabilities.
# A new application type would mean touching every consumer of APP_TYPES, the create form and the
# validator, to distinguish keys that `extension_install_id` already distinguishes.
INSTALL_APP_TYPE = "automation"


# How long a superseded signing secret stays acceptable. Long enough that a receiver can be
# redeployed on its own schedule, short enough that a leaked secret is not honoured for a day.
SIGNING_OVERLAP_SECONDS = 3600.0


def _signing_secret_path(install_id: int) -> str:
    return f"secret/data/{VAULT_EXTENSIONS_PATH}/installations/{install_id}/webhook"


def generate_signing_secret() -> str:
    """A delivery signing secret, independent of the credential that authenticates the add-on."""
    return f"{SIGNING_SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def build_install_metadata(
    *,
    key_id: str,
    extension_slug: str,
    extension_name: str,
    install_id: int,
    organization_id: int,
    organization_slug: Optional[str],
    capabilities: List[str],
    cluster_permissions: List[Dict[str, Any]],
    created_by: str,
    expire_days: int,
    credential_generation: int = 1,
    supersedes: Optional[str] = None,
) -> Dict[str, Any]:
    """The one place an installation's key metadata is constructed.

    important: `capabilities` is always written as a concrete sorted list, and no caller-supplied
    dict is merged in. `granted_capabilities` returns None for a key without the field and
    `has_capability` reads None as unrestricted, so a missing field mints a credential that can do
    everything. A ratchet test checks this single writer.
    """
    issued = now_utc()
    metadata: Dict[str, Any] = {
        "id": key_id,
        "name": f"{extension_slug} @ {organization_slug or organization_id}",
        "app_name": extension_name,
        "app_type": INSTALL_APP_TYPE,
        "capabilities": sorted(set(capabilities)),
        "cluster_permissions": cluster_permissions,
        "created_by": created_by,
        "created_at": utc_isoformat(issued),
        "expires_at": utc_isoformat(issued + timedelta(days=expire_days)),
        "is_active": True,
        "is_revoked": False,
        "last_used_at": None,
        # The join key the reconcile sweep uses to find a credential whose installation is gone.
        "extension_install_id": install_id,
        "extension_slug": extension_slug,
        "organization_id": organization_id,
        "credential_generation": credential_generation,
    }
    if supersedes:
        metadata["supersedes"] = supersedes
    return metadata


def issue_install_credential(
    vault_client,
    *,
    extension_slug: str,
    extension_name: str,
    install_id: int,
    organization_id: int,
    organization_slug: Optional[str],
    capabilities: List[str],
    cluster_permissions: List[Dict[str, Any]],
    created_by: str,
    expire_days: int,
) -> Tuple[str, str, str, Dict[str, Any]]:
    """Mint the credential and the signing secret for a new installation.

    Returns (key_id, key_secret, signing_secret, metadata). Both secrets are returned once and
    are never readable afterwards.
    """
    if not capabilities:
        # The table constraint is the guarantee; this reports the failure at the call site
        # instead of as an integrity error.
        raise ValueError("an installation credential must name at least one capability")

    key_id, key_secret, secret_hash = generate_api_key_pair()
    metadata = build_install_metadata(
        key_id=key_id,
        extension_slug=extension_slug,
        extension_name=extension_name,
        install_id=install_id,
        organization_id=organization_id,
        organization_slug=organization_slug,
        capabilities=capabilities,
        cluster_permissions=cluster_permissions,
        created_by=created_by,
        expire_days=expire_days,
    )

    # important: keep raw_secret None. An installation never carries `app_url`, so nothing reads
    # a recoverable copy and none may be stored.
    save_api_key_to_vault(vault_client, key_id, metadata, secret_hash, raw_secret=None)

    signing_secret = generate_signing_secret()
    vault_client.vault_kv_put(
        _signing_secret_path(install_id), {"signing_secret": signing_secret}
    )

    logger.info("Issued installation credential %s for %s", key_id, extension_slug)
    return key_id, key_secret, signing_secret, metadata


def read_signing_secret(vault_client, install_id: int) -> Optional[str]:
    try:
        payload = vault_client.vault_read(_signing_secret_path(install_id))
    except Exception:
        return None
    return (payload or {}).get("signing_secret")


def read_signing_secrets(vault_client, install_id: int, *, now: Optional[float] = None) -> List[str]:
    """Every secret a delivery should currently be signed with, newest first.

    After a rotation the previous secret stays in the list until its expiry, so `signature_header`
    sends both signatures and a receiver can switch at its own pace instead of rejecting
    deliveries until the subscription reaches AUTO_SUSPEND_AFTER.
    """
    try:
        payload = vault_client.vault_read(_signing_secret_path(install_id)) or {}
    except Exception:
        return []

    secrets_ = [payload.get("signing_secret")]
    previous = payload.get("previous_signing_secret")
    expires_at = payload.get("previous_expires_at")
    if previous and expires_at:
        try:
            still_valid = float(expires_at) > (now if now is not None else time.time())
        except (TypeError, ValueError):
            still_valid = False
        if still_valid:
            secrets_.append(previous)
    return [secret for secret in secrets_ if secret]


def rotate_signing_secret(
    vault_client, install_id: int, *, overlap_seconds: float = SIGNING_OVERLAP_SECONDS
) -> str:
    """Issue a new secret and keep the old one live for a bounded window.

    The window lets a receiver survive a rotation without a redeploy. The expiry is stored beside
    the secret, so no cleanup job is needed.
    """
    previous = read_signing_secret(vault_client, install_id)
    signing_secret = generate_signing_secret()
    payload = {"signing_secret": signing_secret}
    if previous:
        payload["previous_signing_secret"] = previous
        payload["previous_expires_at"] = time.time() + overlap_seconds
    vault_client.vault_kv_put(_signing_secret_path(install_id), payload)
    return signing_secret


def rotate_install_credential(
    vault_client,
    *,
    old_key_id: str,
    extension_slug: str,
    extension_name: str,
    install_id: int,
    organization_id: int,
    organization_slug: Optional[str],
    capabilities: List[str],
    cluster_permissions: List[Dict[str, Any]],
    created_by: str,
    expire_days: int,
    credential_generation: int,
    grace_seconds: int,
) -> Tuple[str, str, Dict[str, Any]]:
    """Mint a new key and retire the old one, optionally leaving it alive for a grace window.

    The old key is retired by shortening its expiry: `validate_api_key` already refuses an expired
    key with 401, so the overlap needs no new enforcement. A grace of zero sets the revoked flag
    instead. The key is never rewritten in place, which would break the add-on the moment the
    operator rotates.
    """
    key_id, key_secret, secret_hash = generate_api_key_pair()
    metadata = build_install_metadata(
        key_id=key_id,
        extension_slug=extension_slug,
        extension_name=extension_name,
        install_id=install_id,
        organization_id=organization_id,
        organization_slug=organization_slug,
        capabilities=capabilities,
        cluster_permissions=cluster_permissions,
        created_by=created_by,
        expire_days=expire_days,
        credential_generation=credential_generation + 1,
        supersedes=old_key_id,
    )
    save_api_key_to_vault(vault_client, key_id, metadata, secret_hash, raw_secret=None)

    _retire_key(vault_client, old_key_id, grace_seconds=grace_seconds)
    logger.info(
        "Rotated installation %s credential %s -> %s (grace %ss)",
        install_id, old_key_id, key_id, grace_seconds,
    )
    return key_id, key_secret, metadata


def _retire_key(vault_client, key_id: str, *, grace_seconds: int) -> None:
    """Shorten a key's life, or revoke it outright when no grace was asked for."""
    # important: a Vault error must raise. Rotation saves the new key first and retires the old one
    # here, so swallowing it would report success while a possibly leaked credential stays live to
    # its original expiry. The caller commits the row only after this returns: on failure
    # `api_key_id` still names the old key, and the reconcile sweep's orphan pass reclaims the new.
    try:
        # Uncached: rotation holds this key's lock, and the write below replaces the whole document.
        metadata, _ = get_api_key_from_vault(vault_client, key_id, use_cache=False)
    except Exception as exc:
        logger.warning("Could not read %s to retire it: %s", key_id, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "The credential store is unavailable, so the previous credential was not "
                "retired and this change was not applied. The existing credential still works."
            ),
        ) from exc

    if grace_seconds > 0:
        metadata["expires_at"] = utc_isoformat(now_utc() + timedelta(seconds=grace_seconds))
    else:
        metadata["is_revoked"] = True
        metadata["is_active"] = False
        metadata["revoked_at"] = utc_isoformat(now_utc())

    vault_client.vault_kv_put(
        f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata", metadata
    )
    # Each worker holds its own validation cache; the eviction tombstone applies this at once
    # instead of after a per-process TTL.
    evict_api_key_cache(key_id)


def revoke_install_credential(vault_client, key_id: str) -> None:
    """Stop a credential working now, without deleting the record of what it was."""
    _retire_key(vault_client, key_id, grace_seconds=0)


def destroy_install_credential(vault_client, key_id: str) -> bool:
    """Remove the key entirely. Uninstall only, where nothing should be left to read.

    True only when Vault holds nothing of it, so the teardown records only what it removed.
    """
    removed = delete_api_key_from_vault(vault_client, key_id)
    evict_api_key_cache(key_id)
    return removed


def destroy_signing_secret(vault_client, install_id: int) -> bool:
    """True when nothing is left behind. Already gone counts, an unknown failure does not.

    The teardown writes what it removed into the uninstall audit record, so a secret still in
    Vault must not be reported as destroyed.
    """
    from app.core.vault_client import VaultClientError

    try:
        vault_client.vault_delete(_signing_secret_path(install_id))
        return True
    except VaultClientError:
        logger.warning("Signing secret for installation %s was already gone", install_id)
        return True
    except Exception:
        logger.warning(
            "Could not delete the signing secret for installation %s", install_id, exc_info=True
        )
        return False
