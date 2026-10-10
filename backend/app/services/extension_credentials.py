"""Issuing, rotating and retiring the credential that belongs to one installation.

Every Vault operation here reuses the integration key machinery rather than reimplementing it, so
an installation credential authenticates through exactly the same path as any other key and
inherits its cache, its cross-worker revocation and its per-key rate limit for free.

Two differences from an ordinary key, and both are the point.

First, hash-only. `save_api_key_to_vault` is called with `raw_secret=None`, so no recoverable copy
of the secret is ever written. That claim is only true because the backfill in
`_validate_api_key_blocking` is now guarded on `app_url`, which an installation credential never
has; without that guard the add-on's first request would have quietly undone it.

Second, a separate signing secret. The existing OrcaHub emitter signs deliveries with the API
key's own secret, which means anyone who can merely VERIFY a signature is holding a live
control-plane credential. With one partner that is one receiver. With an open platform it is every
author, so the two are split: one authenticates the add-on to us, the other proves a delivery came
from us, and rotating either leaves the other alone.
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

    important: `capabilities` is written as a concrete sorted list, always, and this function
    never merges a caller-supplied dict that could omit it. `granted_capabilities` returns None
    for a key whose field is absent and `has_capability` reads None as unrestricted, so a single
    path that forgets the key mints a credential that can do everything. One writer means there
    is one place to check, and a ratchet checks it.
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
        # Belt to the database's braces. The table constraint is the real guarantee; this makes
        # the failure legible at the call site rather than as an integrity error.
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

    # important: raw_secret is not passed. An installation never carries `app_url`, so nothing
    # reads a recoverable copy and nothing writes one.
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

    important: this exists because rotation had no overlap at all. `signature_header` takes a LIST
    precisely so both signatures ride during a rotation, and the subscription row has carried
    `previous_secret_expires_at` since it was created, with a comment saying a receiver switches
    at its own pace. But only one secret was ever stored and rotation overwrote it in place, so
    every receiver that had not already been handed the new value started rejecting on the very
    next delivery and walked toward AUTO_SUSPEND_AFTER. The mechanism was built and never
    connected to anything.
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

    The window is what makes a rotation something a receiver survives rather than something it
    has to be redeployed for. It is bounded because an old secret that never expires is not a
    rotation, and the expiry is stored beside the secret so nothing has to remember to clean up.
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

    important: the old key is retired by SHORTENING ITS EXPIRY, not by adding a new mechanism.
    `validate_api_key` already refuses an expired key with 401, so an overlap window costs zero
    new enforcement code and cannot be got wrong in a way the existing auth path would miss. A
    grace of zero revokes outright instead, because an expiry in the past is a stranger shape
    than the flag that already means this.

    Never overwrites in place. An in-place rewrite has no overlap at all and breaks the add-on at
    the instant the operator clicks the button.
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
    # important: this must not be skipped. Rotation saves the new key first and retires the old
    # one here, so swallowing a Vault error completed the rotation while leaving the superseded
    # credential live to its original expiry, a year by default. Rotation is what an operator does
    # when a credential may have leaked, so reporting success there is the worst possible answer.
    #
    # Raising is safe: the caller commits the row after this returns, so a failure leaves
    # `api_key_id` pointing at the old key, which still works, and the newly written key is
    # claimed by nothing and reclaimed by the reconcile sweep's orphan pass.
    try:
        # note: uncached: rotation holds this key's lock, and the write below is the whole document
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
    # Every worker holds its own validation cache, so the tombstone is what makes this immediate
    # rather than eventual on a per-process TTL.
    evict_api_key_cache(key_id)


def revoke_install_credential(vault_client, key_id: str) -> None:
    """Stop a credential working now, without deleting the record of what it was."""
    _retire_key(vault_client, key_id, grace_seconds=0)


def destroy_install_credential(vault_client, key_id: str) -> None:
    """Remove the key entirely. Uninstall only, where nothing should be left to read."""
    delete_api_key_from_vault(vault_client, key_id)
    evict_api_key_cache(key_id)


def destroy_signing_secret(vault_client, install_id: int) -> bool:
    """True when nothing is left behind. Already gone counts, an unknown failure does not.

    important: it returns an answer now because the teardown records what it removed into the
    uninstall audit record, and it used to record this one unconditionally. Secret material left
    in Vault while the operator is told it was destroyed is the worst entry on that list to have
    wrong.
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
