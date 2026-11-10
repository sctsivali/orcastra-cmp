"""Removing an installation, and proving nothing is left behind.

An installation writes to Vault, to Redis, to process memory on every worker, and to two Postgres
tables that reference it only by a free-text id. The artefacts are listed in one table, and a
ratchet test asserts that every writer named there has a remover.

important: Vault first, row last. If the row delete fails after the Vault delete, the add-on gets
401. The other order could leave a live credential with no row describing it, so the surviving
failure mode is denial rather than continued access.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.routers.integrations import VAULT_INTEGRATIONS_PATH
from app.services import api_key_usage
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


# The inventory a ratchet test reads. An entry with `remover=None` marks an artefact that is meant
# to stay after an uninstall.
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
    "postgres:api_key_usage": Artefact(
        "postgres", "record_use", "teardown_install",
        "when the credential last authenticated, kept apart from the key document",
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

    Returns the artefact keys that were acted on, so the caller logs what happened.
    """
    done: List[str] = []

    # Off the event loop (one Vault call per record under the key), and recorded only when Vault
    # confirms nothing of the key is left, as for the two entries below.
    if await asyncio.to_thread(lambda: destroy_install_credential(vault_client, api_key_id)):
        done.append("vault:api_key_metadata")
        done.append("vault:api_key_secret")
    done.append("process:key_metadata_cache")

    if previous_api_key_id:
        if await asyncio.to_thread(lambda: destroy_install_credential(vault_client, previous_api_key_id)):
            done.append("vault:superseded_api_key")

    forgotten = [await api_key_usage.forget(key_id) for key_id in filter(None, (api_key_id, previous_api_key_id))]
    if all(forgotten):
        done.append("postgres:api_key_usage")

    # important: these two swallow their own failures, so they are recorded only on a True result.
    # Otherwise a Vault failure would leave secret material behind while the audit record says it
    # was destroyed.
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

    # Expire the sessions so the existing upload GC removes the staged bytes; this module never
    # needs to know what a staging path is.
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

    # Revoke grants naming this key: cluster_id is free text with no foreign key, so a leftover
    # grant would match the same id registered again by a different owner.
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
    """The entries meant to stay, for a test to compare with a written expectation."""
    return {name: a for name, a in INSTALL_ARTEFACTS.items() if a.remover is None}
