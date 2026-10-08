"""Installing an add-on into one organization, and everything that follows from it.

The consent preview is computed here, on the server, and echoed back to the browser. That is
deliberate: a browser-computed intersection that disagrees with the server is a lie told to an
operator at the exact moment they are being asked to approve something.
"""

import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capabilities import CAPABILITIES
from app.core.api_key_capability_audit import capability_audit_fields
from app.core.api_key_expiry import now_utc
from app.core.config import get_settings
from app.core.org_scope import get_current_allowed_clusters
from app.models.db_models import Organization
from app.models.extension_models import Extension, ExtensionInstallation
from app.models.extension_schemas import (
    InstallResponse,
    capability_request_info,
)
from app.services import extension_service, extension_settings
from app.services.extension_credentials import (
    issue_install_credential,
    rotate_install_credential,
)
from app.services.extension_scope import (
    assert_install_scope_within_partner,
    evict_install_cache,
)
from app.services.extension_teardown import teardown_install
from app.services.integration_key_ownership import (
    carry_owned_organizations,
    record_carried_organizations,
    refuse_if_key_owns_organizations,
    serialize_key_ownership,
)

logger = logging.getLogger(__name__)


async def _organization(db: AsyncSession, organization_id: int) -> Organization:
    result = await db.execute(select(Organization).where(Organization.id == organization_id))
    organization = result.scalar_one_or_none()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


def to_response(
    install: ExtensionInstallation,
    extension: Extension,
    organization_slug: Optional[str],
    *,
    credential: Optional[Dict[str, Any]] = None,
) -> InstallResponse:
    """What an installation is, as its operator should see it.

    important: `extension_version` is the version THIS installation consented to, not whatever the
    add-on has published since. Reporting the current one made an install that agreed to 1.0.0
    display 2.0.0, which is the opposite of what a consent record is for. The newer version is
    reported separately as something available, so the difference is visible rather than hidden.
    """
    consented = install.consent_version or extension.version
    return InstallResponse(
        id=install.id,
        extension_id=install.extension_id,
        extension_slug=extension.slug,
        extension_name=extension.name,
        extension_version=consented,
        available_version=extension.version if extension.version != consented else None,
        organization_id=install.organization_id,
        organization_slug=organization_slug,
        status=install.status,
        granted_capabilities=list(install.granted_capabilities or []),
        granted_scope=list(install.granted_scope or []),
        settings=dict(install.settings or {}),
        configured_secrets=list(install.configured_secrets or []),
        api_key_id=install.api_key_id,
        credential_generation=install.credential_generation,
        consent_version=install.consent_version,
        consented_at=install.consented_at,
        installed_by=install.installed_by,
        suspended_at=install.suspended_at,
        suspended_by=install.suspended_by,
        suspend_reason=install.suspend_reason,
        # An operator asking "is this add-on working" needs a signal, and the only honest one we
        # hold is when its credential last authenticated. Absent when the key could not be read,
        # which is itself worth showing rather than rendering as "never used".
        credential_last_used_at=(credential or {}).get("last_used_at"),
        credential_expires_at=(credential or {}).get("expires_at"),
        credential_readable=credential is not None,
        created_at=install.created_at,
        updated_at=install.updated_at,
    )


# ── the consent preview ────────────────────────────────────────────────────────────────────────

async def build_install_preview(
    db: AsyncSession, extension: Extension, *, organization_id: int
) -> Dict[str, Any]:
    """What the operator is being asked to approve, with the intersection already applied.

    The `granted` column of the consent screen is this, not something the browser worked out.
    Rows the add-on asked for and will not get are returned explicitly so the screen can say so
    rather than quietly showing a shorter list.
    """
    organization = await _organization(db, organization_id)
    allowed = get_current_allowed_clusters()

    reachable: List[str] = []
    refused: List[str] = []
    for cluster_id in await _clusters_bound_to(db, organization_id):
        if allowed is None or cluster_id in allowed:
            reachable.append(cluster_id)
        else:
            refused.append(cluster_id)

    requested = list(extension.requested_capabilities or [])
    return {
        "extension": extension_service.to_response(extension),
        "organization_id": organization.id,
        "organization_slug": organization.slug,
        "capabilities": [
            capability_request_info(name, CAPABILITIES.get(name, ""))
            for name in requested
        ],
        "grantable_cluster_ids": sorted(reachable),
        "refused_cluster_ids": sorted(refused),
        "requested_scope": dict(extension.requested_scope or {}),
        "settings_schema": list(extension.settings_schema or []),
        "manifest_digest": extension.manifest_digest,
        "version": extension.version,
    }


async def _clusters_bound_to(db: AsyncSession, organization_id: int) -> List[str]:
    """The cluster ids this organization is bound to.

    note: ids only. `OrganizationCluster` carries no display name; the cluster id IS the Vault
    key and the name a cluster is known by everywhere else in this codebase. An earlier version
    of this selected a `cluster_name` column that does not exist, which is a 500 no unit test
    could see because they fake the session.
    """
    from app.models.db_models import OrganizationCluster

    result = await db.execute(
        select(OrganizationCluster.cluster_id).where(
            OrganizationCluster.organization_id == organization_id
        )
    )
    return [row[0] for row in result.all() if row[0]]


# ── installing ─────────────────────────────────────────────────────────────────────────────────

async def install_extension(
    db: AsyncSession,
    vault_client,
    *,
    extension: Extension,
    user,
    is_admin: bool,
    organization_id: int,
    consent_version: str,
    consent_digest: str,
    capabilities: Sequence[str],
    grants: Sequence[Any],
    settings_values: Optional[Dict[str, Any]] = None,
) -> Tuple[ExtensionInstallation, str, str, str]:
    """Consent, scope, credential, row. Returns (install, key_id, secret, signing_secret)."""
    settings = get_settings()

    if extension.status not in ("published", "draft"):
        raise HTTPException(
            status_code=409, detail=f"This add-on is {extension.status} and cannot be installed"
        )

    # important: the digest the operator was shown, recomputed from what is stored now. Without
    # this the attack is publish something benign, wait for the consent screen to render, change
    # the manifest to ask for more, and let the install request land against the new one.
    if not extension_service.manifest_matches(extension, consent_digest):
        raise HTTPException(
            status_code=409,
            detail=(
                "This add-on changed while you were reading it. Review the new version before "
                "installing."
            ),
        )
    if consent_version != extension.version:
        raise HTTPException(
            status_code=409,
            detail="This add-on changed version while you were reading it. Review it again.",
        )

    # An installation may never hold more than the manifest declared it needs.
    over_reach = sorted(set(capabilities) - set(extension.requested_capabilities or []))
    if over_reach:
        raise HTTPException(
            status_code=422,
            detail=f"This add-on did not ask for: {', '.join(over_reach)}",
        )

    await assert_install_scope_within_partner(
        db, user,
        organization_id=organization_id,
        grants=grants,
        manifest_scope=dict(extension.requested_scope or {}),
        capabilities=capabilities,
        is_admin=is_admin,
    )

    existing = await db.execute(
        select(ExtensionInstallation).where(
            ExtensionInstallation.extension_id == extension.id,
            ExtensionInstallation.organization_id == organization_id,
        )
    )
    if (already := existing.scalar_one_or_none()) is not None:
        # Deliberately not an upsert. Re-issuing silently would be an unrequested credential
        # rotation, and the add-on holding the old one would stop working with no explanation.
        raise HTTPException(
            status_code=409,
            detail=f"Already installed in this organization (installation {already.id})",
        )

    if settings.extension_max_installs_per_org:
        count = await db.execute(
            select(func.count())
            .select_from(ExtensionInstallation)
            .where(ExtensionInstallation.organization_id == organization_id)
        )
        if count.scalar_one() >= settings.extension_max_installs_per_org:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This organization has reached its add-on limit "
                    f"({settings.extension_max_installs_per_org})"
                ),
            )

    organization = await _organization(db, organization_id)
    grant_dicts = [g.model_dump() if hasattr(g, "model_dump") else dict(g) for g in grants]

    plain, secret_values = extension_settings.validate_values(
        extension.settings_schema, settings_values or {}
    )

    # The row first, so the credential can carry its id, then the credential. A failure between
    # the two leaves an installation with no key, which reads as broken rather than as access.
    install = ExtensionInstallation(
        extension_id=extension.id,
        organization_id=organization_id,
        status="active",
        granted_capabilities=sorted(set(capabilities)),
        granted_scope=grant_dicts,
        settings=plain or None,
        api_key_id=f"pending-{extension.slug}-{organization_id}",
        consent_version=consent_version,
        consent_manifest_digest=consent_digest,
        # The document, not only its hash. A later version replaces the manifest on the extension
        # row, so this is the only surviving record of what this organization actually approved.
        consented_manifest=dict(extension.manifest or {}),
        installed_by=getattr(user, "email", "unknown"),
    )
    db.add(install)
    await db.flush()

    key_id, key_secret, signing_secret, _metadata = issue_install_credential(
        vault_client,
        extension_slug=extension.slug,
        extension_name=extension.name,
        install_id=install.id,
        organization_id=organization_id,
        organization_slug=organization.slug,
        capabilities=list(install.granted_capabilities),
        cluster_permissions=grant_dicts,
        created_by=getattr(user, "email", "unknown"),
        expire_days=settings.extension_credential_expire_days,
    )
    install.api_key_id = key_id
    if secret_values:
        install.configured_secrets = extension_settings.store_secrets(
            vault_client, install.id, secret_values
        )
    await db.commit()
    await db.refresh(install)

    logger.info(
        "Installed %s into organization %s as %s", extension.slug, organization_id, key_id
    )
    return install, key_id, key_secret, signing_secret


# ── changing an installation ───────────────────────────────────────────────────────────────────

async def rescope_install(
    db: AsyncSession,
    vault_client,
    *,
    install: ExtensionInstallation,
    extension: Extension,
    user,
    is_admin: bool,
    consent_version: str,
    consent_digest: str,
    capabilities: Sequence[str],
    grants: Sequence[Any],
    # note: filled with what the credential write changed, for the caller's audit record
    audit_fields: Optional[Dict[str, Any]] = None,
) -> ExtensionInstallation:
    """Declarative: the request states the whole grant, so a repeat converges."""
    if not extension_service.manifest_matches(extension, consent_digest):
        raise HTTPException(
            status_code=409, detail="This add-on changed while you were reading it."
        )
    over_reach = sorted(set(capabilities) - set(extension.requested_capabilities or []))
    if over_reach:
        raise HTTPException(
            status_code=422, detail=f"This add-on did not ask for: {', '.join(over_reach)}"
        )

    await assert_install_scope_within_partner(
        db, user,
        organization_id=install.organization_id,
        grants=grants,
        manifest_scope=dict(extension.requested_scope or {}),
        capabilities=capabilities,
        is_admin=is_admin,
    )

    grant_dicts = [g.model_dump() if hasattr(g, "model_dump") else dict(g) for g in grants]
    install.granted_capabilities = sorted(set(capabilities))
    install.granted_scope = grant_dicts
    install.consent_version = consent_version
    install.consent_manifest_digest = consent_digest
    # Rescoping is a fresh consent, so the snapshot moves with it. Leaving the old one would make
    # the record claim agreement to a document this operator never saw.
    install.consented_manifest = dict(extension.manifest or {})
    install.last_scope_change_at = now_utc()

    # The Vault key carries the same grant, because that is what the request path reads.
    key_change = _rewrite_key_grant(vault_client, install)
    if audit_fields is not None:
        audit_fields.update(key_change)

    from app.services.webhooks.emitter import emit_installation_event

    await emit_installation_event(
        db, event_type="orcastra.install.scope_changed.v1", installation=install
    )
    await db.commit()
    await db.refresh(install)
    evict_install_cache(install.api_key_id)
    return install


def _rewrite_key_grant(vault_client, install: ExtensionInstallation) -> Dict[str, Any]:
    from app.routers.integrations import (
        VAULT_INTEGRATIONS_PATH,
        evict_api_key_cache,
        get_api_key_from_vault,
    )

    # important: this must fail the whole rescope rather than be skipped. The request path reads
    # capabilities and clusters from the Vault key, not from the row, so swallowing a Vault error
    # here committed the narrower grant to the database while the credential kept the wider one.
    # A narrowing is a security control, and it reported success while doing nothing: the operator
    # saw the reduced scope and the add-on kept the old reach. Raising leaves the row uncommitted,
    # since the caller commits immediately after this returns.
    try:
        metadata, _ = get_api_key_from_vault(vault_client, install.api_key_id)
    except Exception as exc:
        logger.warning("Could not read %s to rewrite its grant: %s", install.api_key_id, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "The credential store is unavailable, so this change was not applied. "
                "Nothing was altered. Try again once it is reachable."
            ),
        ) from exc
    # important: the key's own set before the write, not the row's. A key edited through the
    # integration capabilities route differs from its row, and this rewrite then withdraws that.
    previous = dict(metadata)
    metadata["capabilities"] = list(install.granted_capabilities or [])
    metadata["cluster_permissions"] = list(install.granted_scope or [])
    try:
        vault_client.vault_kv_put(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{install.api_key_id}/metadata",
            metadata,
        )
    except Exception as exc:
        logger.warning("Could not rewrite the grant on %s: %s", install.api_key_id, exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "The credential store is unavailable, so this change was not applied. "
                "Nothing was altered. Try again once it is reachable."
            ),
        ) from exc
    evict_api_key_cache(install.api_key_id)
    return capability_audit_fields(metadata, previous)


async def set_install_status(
    db: AsyncSession,
    *,
    install: ExtensionInstallation,
    status: str,
    actor: str,
    reason: Optional[str] = None,
) -> ExtensionInstallation:
    install.status = status
    if status == "suspended":
        install.suspended_at = now_utc()
        install.suspended_by = actor
        install.suspend_reason = reason
    else:
        install.suspended_at = None
        install.suspended_by = None
        install.suspend_reason = None

    # important: enqueued BEFORE the commit, so the event and the status move together. An add-on
    # is never told it was suspended by a suspension that then rolled back, and a suspension never
    # lands silently. Both catalogued events are sourced here because this is the one function
    # that changes an installation's status.
    from app.services.webhooks.emitter import emit_installation_event

    await emit_installation_event(
        db,
        event_type=(
            "orcastra.install.suspended.v1" if status == "suspended"
            else "orcastra.install.resumed.v1"
        ),
        installation=install,
    )
    await db.commit()
    await db.refresh(install)
    # The row moved without the Vault key moving, so the key cache's own tombstone would not
    # notice. This is what makes a suspension immediate on all four workers.
    evict_install_cache(install.api_key_id)
    return install


async def rotate_credential(
    db: AsyncSession,
    vault_client,
    *,
    install: ExtensionInstallation,
    extension: Extension,
    organization_slug: Optional[str],
    actor: str,
    grace_seconds: int,
) -> Tuple[ExtensionInstallation, str, str]:
    settings = get_settings()

    if install.last_scope_change_at is None:
        last = install.updated_at
    else:
        last = max(install.updated_at, install.last_scope_change_at)
    floor = timedelta(seconds=settings.extension_credential_min_rotate_seconds)
    if install.credential_generation > 1 and last and (now_utc() - last) < floor:
        raise HTTPException(
            status_code=429,
            detail=(
                "This credential was rotated moments ago. Wait "
                f"{settings.extension_credential_min_rotate_seconds}s before rotating again."
            ),
        )

    grace = max(0, min(grace_seconds, settings.extension_credential_max_grace_seconds))
    old_key_id = install.api_key_id
    new_key_id, new_secret, _metadata = rotate_install_credential(
        vault_client,
        old_key_id=old_key_id,
        extension_slug=extension.slug,
        extension_name=extension.name,
        install_id=install.id,
        organization_id=install.organization_id,
        organization_slug=organization_slug,
        capabilities=list(install.granted_capabilities or []),
        cluster_permissions=list(install.granted_scope or []),
        created_by=actor,
        expire_days=settings.extension_credential_expire_days,
        credential_generation=install.credential_generation,
        grace_seconds=grace,
    )
    install.previous_api_key_id = old_key_id if grace else None
    install.previous_key_expires_at = (
        now_utc() + timedelta(seconds=grace) if grace else None
    )
    install.api_key_id = new_key_id
    install.credential_generation += 1
    # important: the organizations the old credential owns move to the new one in the same commit
    # as the row. Only the owner key may sync an organization, so leaving them on a credential this
    # rotation retires would freeze them, and the add-on's next sync with its new key would be
    # refused for every one of them.
    async with serialize_key_ownership(db, old_key_id):
        carried = await carry_owned_organizations(db, from_key_id=old_key_id, to_key_id=new_key_id)
        await db.commit()
    await db.refresh(install)
    evict_install_cache(old_key_id)
    record_carried_organizations(
        carried, from_key_id=old_key_id, to_key_id=new_key_id, trigger="extension.install.credential.rotate"
    )
    return install, new_key_id, new_secret


async def uninstall(
    db: AsyncSession, vault_client, *, install: ExtensionInstallation, user: Any
) -> List[str]:
    """Remove an installation and everything it created.

    important: refused (409 key_owns_organizations) while either of its credentials owns an active
    OrcaHub organization. Destroying the owner key freezes those organizations, and suspending the
    add-on stops it at once without that, so the refusal names suspension as the stop and a
    transfer or a delete of the organizations as the way forward.
    """
    keys = sorted(filter(None, {install.api_key_id, install.previous_api_key_id}))
    async with AsyncExitStack() as held:
        # note: sorted, so two requests that lock the same pair always take them in one order
        for key_id in keys:
            await held.enter_async_context(serialize_key_ownership(db, key_id))
        for key_id in keys:
            await refuse_if_key_owns_organizations(
                db, key_id, action="uninstall", user=user, metadata={"extension_install_id": install.id}
            )
        removed = await teardown_install(
            db, vault_client,
            install_id=install.id,
            api_key_id=install.api_key_id,
            previous_api_key_id=install.previous_api_key_id,
        )
        await db.commit()
    return removed


async def update_settings(
    db: AsyncSession,
    vault_client,
    *,
    install: ExtensionInstallation,
    extension: Extension,
    values: Dict[str, Any],
) -> ExtensionInstallation:
    plain, secret_values = extension_settings.validate_values(
        extension.settings_schema, values, already_configured=install.configured_secrets or []
    )
    install.settings = plain or None
    if secret_values:
        install.configured_secrets = extension_settings.store_secrets(
            vault_client, install.id, secret_values
        )
    await db.commit()
    await db.refresh(install)
    evict_install_cache(install.api_key_id)
    return install


async def get_install_or_404(
    db: AsyncSession, install_id: int, *, allowed_org_ids: Optional[List[int]]
) -> ExtensionInstallation:
    """Subset semantics, deliberately.

    `_authorize_api_key` starts from overlap: a key is manageable if its clusters intersect the
    caller's, narrowed by party only where another party also holds one of them
    (services/cluster_parties.py). This surface does not inherit that, because overlap would let
    a partner sharing one cluster enumerate and delete another partner's installations.
    """
    result = await db.execute(
        select(ExtensionInstallation).where(ExtensionInstallation.id == install_id)
    )
    install = result.scalar_one_or_none()
    if install is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    if allowed_org_ids is not None and install.organization_id not in allowed_org_ids:
        raise HTTPException(status_code=404, detail="Installation not found")
    return install
