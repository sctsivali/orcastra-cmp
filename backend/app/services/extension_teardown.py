"""Removing an installation, and proving nothing is left behind.

Deleting the row is the easy part. An installation writes to Vault, to Redis, to process memory
on four workers, and to two Postgres tables that reference it only by a free-text id. Cluster
deletion in this codebase has already shipped the shape of this bug twice, so the artefacts are
enumerated in one table and a ratchet asserts every writer named there has a remover.

important: Vault first, row last. If the Vault delete succeeds and the row delete fails, the
add-on gets 401 and someone files a bug. The other order leaves a live credential with nothing
describing it, which is a security failure nobody notices. Order for fail-safety, so the surviving
failure mode is denial rather than continued access.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.routers.integrations import VAULT_INTEGRATIONS_PATH
from app.services.extension_credentials import (
    destroy_install_credential,
    destroy_signing_secret,
)
from app.services.extension_settings import destroy_settings
from app.services.extension_scope import evict_install_cache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Artefact:
    """One thing an installation creates, and the call that removes it."""

    store: str
    writer: str
    remover: Optional[str]
    note: str = ""


# The inventory. A ratchet reads this, so an entry with `remover=None` is a deliberate,
# reviewable statement that something is kept, not an omission.
INSTALL_ARTEFACTS: Dict[str, Artefact] = {
    "vault:api_key_metadata": Artefact(
        "vault", "save_api_key_to_vault", "destroy_install_credential"
    ),
    "vault:api_key_secret": Artefact(
        "vault", "save_api_key_to_vault", "destroy_install_credential"
    ),
    "vault:webhook_signing_secret": Artefact(
        "vault", "issue_install_credential", "destroy_signing_secret"
    ),
    "vault:settings_secrets": Artefact(
        "vault", "store_secrets", "destroy_settings",
        "values for the manifest's secret fields, write-only and never read back to a browser",
    ),
    "vault:superseded_api_key": Artefact(
        "vault", "rotate_install_credential", "destroy_install_credential",
        "the previous generation, still alive inside its grace window",
    ),
    "redis:vault_descriptor_cache": Artefact(
        "redis", "get_api_key_from_vault", "teardown_install",
        "the descriptor path is cacheable; the credential path is not, and is asserted so",
    ),
    "redis:api_key_list_responses": Artefact(
        "redis", "list_api_keys", "teardown_install"
    ),
    "redis:api_key_revocation_tombstone": Artefact(
        "redis", "evict_api_key_cache", None, "self-expiring, and its whole job is to outlive"
    ),
    "redis:install_tombstone": Artefact(
        "redis", "evict_install_cache", None, "self-expiring, same reason"
    ),
    "redis:rate_limit_bucket": Artefact(
        "redis", "consume_api_key_budget", None, "self-expiring within the window"
    ),
    "process:key_metadata_cache": Artefact(
        "process", "_store_key_metadata", "destroy_install_credential",
        "evicted across workers by the tombstone, not by reaching into them",
    ),
    "process:install_snapshot_cache": Artefact(
        "process", "load_install_snapshot", "evict_install_cache"
    ),
    "postgres:extension_installation_row": Artefact(
        "postgres", "install_extension", "teardown_install"
    ),
    "postgres:webhook_subscriptions": Artefact(
        "postgres", "create_subscription", "teardown_install",
        "removed by the installation's own foreign key, which is CASCADE here because a "
        "subscription owns no credential material of its own",
    ),
    "postgres:webhook_deliveries": Artefact(
        "postgres", "enqueue_event", "teardown_install",
        "CASCADE from the subscription, so the delivery log goes with the thing it logged",
    ),
    "postgres:image_upload_sessions": Artefact(
        "postgres", "create_image_upload_session", "teardown_install",
        "expired rather than deleted, so the existing GC removes the staged bytes on disk",
    ),
    "postgres:iso_upload_sessions": Artefact(
        "postgres", "create_iso_upload_session", "teardown_install",
        "same, and the staging path is not this module's business",
    ),
    "postgres:break_glass_grants": Artefact(
        "postgres", "create_grant", "teardown_install",
        "a grant whose principal is a key that no longer exists would authorize nothing "
        "identifiable, and would survive a re-registration of the same cluster id",
    ),
    "opensearch:audit_records": Artefact(
        "opensearch", "log_activity", None,
        "kept deliberately. They are the record that the installation existed, and the tenant "
        "reads them to see what reached their instances.",
    ),
}


async def teardown_install(
    db: AsyncSession,
    vault_client,
    *,
    install_id: int,
    api_key_id: str,
    previous_api_key_id: Optional[str] = None,
) -> List[str]:
    """Remove everything an installation created, in fail-safe order.

    Returns the artefact keys that were acted on, so a caller can log what actually happened
    rather than asserting that it did.
    """
    done: List[str] = []

    destroy_install_credential(vault_client, api_key_id)
    done.append("vault:api_key_metadata")
    done.append("vault:api_key_secret")
    done.append("process:key_metadata_cache")

    if previous_api_key_id:
        destroy_install_credential(vault_client, previous_api_key_id)
        done.append("vault:superseded_api_key")

    # important: these two swallow their own failures, so calling them proves nothing. They are
    # the entries that matter most, because a Vault-side failure leaves real secret material
    # behind while the audit record says it was destroyed.
    if destroy_signing_secret(vault_client, install_id):
        done.append("vault:webhook_signing_secret")
    if destroy_settings(vault_client, install_id):
        done.append("vault:settings_secrets")

    cache = get_cache_manager()
    cleared = True
    for key_id in filter(None, (api_key_id, previous_api_key_id)):
        try:
            cache.delete_pattern(
                "vault", f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/*"
            )
        except Exception:
            cleared = False
            logger.warning("Could not clear cached descriptors for %s", key_id, exc_info=True)
    if cleared:
        done.append("redis:vault_descriptor_cache")

    try:
        cache.delete_pattern("api_responses", "integrations:api_keys:list*")
        done.append("redis:api_key_list_responses")
    except Exception:
        logger.warning("Could not clear the cached key list", exc_info=True)

    evict_install_cache(api_key_id)
    if previous_api_key_id:
        evict_install_cache(previous_api_key_id)
    done.append("process:install_snapshot_cache")

    # Hand the staged bytes to the sweep that already knows how to remove them. Expiring the row
    # rather than deleting it means this module never has to know what a staging path is.
    for table in ("image_upload_sessions", "iso_upload_sessions"):
        try:
            await db.execute(
                text(
                    f"UPDATE {table} SET expires_at = now() "
                    "WHERE api_key_id = :key AND status NOT IN ('ready', 'failed')"
                ),
                {"key": api_key_id},
            )
            done.append(f"postgres:{table}")
        except Exception:
            logger.warning("Could not expire %s rows for %s", table, api_key_id, exc_info=True)

    # A grant naming a key that no longer exists is worse than useless: cluster_id is free text
    # with no foreign key, so the same id registered again by a different owner matches it.
    try:
        await db.execute(
            text(
                "UPDATE break_glass_grants "
                "SET revoked_at = now(), status = 'revoked' "
                "WHERE api_key_id = :key AND revoked_at IS NULL"
            ),
            {"key": api_key_id},
        )
        done.append("postgres:break_glass_grants")
    except Exception:
        logger.warning("Could not revoke break-glass grants for %s", api_key_id, exc_info=True)

    await db.execute(
        text("DELETE FROM extension_installations WHERE id = :id"), {"id": install_id}
    )
    done.append("postgres:extension_installation_row")

    return done


def artefacts_without_a_remover() -> Dict[str, Artefact]:
    """The entries deliberately kept, for a test to assert against a written expectation."""
    return {name: a for name, a in INSTALL_ARTEFACTS.items() if a.remover is None}
