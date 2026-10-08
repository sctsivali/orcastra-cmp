"""Installing an add-on into an organization, and everything an operator does to one afterwards.

Every write here is audited with the operator as the actor, because these are the actions that
hand a third party access to a customer's infrastructure and take it away again.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capability_audit import issued_credential_audit_fields
from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_partner
from app.core.org_scope import get_user_org_ids, org_scope_dependency
from app.core.rbac import Role
from app.models.db_models import Organization
from app.models.extension_models import Extension, ExtensionInstallation
from app.models.extension_schemas import (
    InstallCreateResponse,
    InstallRequest,
    InstallResponse,
    ScopeRequest,
    SettingsUpdate,
)
from app.routers.integrations import get_vault_integrations_client
from app.services import extension_install, extension_service
from app.services.activity_logger import get_activity_logger
from app.services.integration_key_ownership import KeyOwnsOrganizations

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/extensions",
    tags=["Extensions"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)


def _is_admin(user: AuthenticatedUser) -> bool:
    return Role.ADMIN.value in (user.roles or [])


async def _allowed_org_ids(user: AuthenticatedUser, db: AsyncSession) -> Optional[List[int]]:
    """None means every organization, which only an admin gets."""
    if _is_admin(user):
        return None
    return await get_user_org_ids(user, db)


def _audit(
    action: str,
    install_id: Any,
    *,
    organization_id: Optional[int] = None,
    organization_slug: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    result: str = "success",
    error_code: Optional[str] = None,
) -> None:
    """The operator is the actor; log_activity reads them from the request context.

    important: `target_organization_id` is passed so the record lands attributable to the tenant
    that was acted upon, which is what makes it visible in a tenant-scoped view of the audit
    rather than only to whoever ran the action.
    """
    try:
        activity_logger.log_activity(
            action=action,
            target_type="extension_installation",
            target_id=str(install_id),
            target_organization_id=organization_id,
            target_organization_slug=organization_slug,
            result=result,
            error_code=error_code,
            details=details or {},
        )
    except Exception:  # pragma: no cover
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


async def _load(
    db: AsyncSession, install_id: int, user: AuthenticatedUser
) -> tuple[ExtensionInstallation, Extension, Optional[str]]:
    install = await extension_install.get_install_or_404(
        db, install_id, allowed_org_ids=await _allowed_org_ids(user, db)
    )
    extension = (
        await db.execute(select(Extension).where(Extension.id == install.extension_id))
    ).scalar_one()
    organization = (
        await db.execute(
            select(Organization).where(Organization.id == install.organization_id)
        )
    ).scalar_one_or_none()
    return install, extension, organization.slug if organization else None


# ── the consent preview ────────────────────────────────────────────────────────────────────────

@router.get(
    "/{slug}/install-preview",
    summary="What installing this add-on would grant",
    description=(
        "The intersection of what the add-on asks for and what you hold, computed here rather "
        "than in the browser so the consent screen cannot show something the server disagrees "
        "with."
    ),
)
async def install_preview(
    slug: str,
    organization_id: int = Query(...),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    extension = await extension_service.get_extension_or_404(
        db, slug, caller_email=user.email, is_admin=_is_admin(user)
    )
    allowed = await _allowed_org_ids(user, db)
    if allowed is not None and organization_id not in allowed:
        # Same shape as everywhere else: not visible is not distinguishable from not there.
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Organization not found")
    return await extension_install.build_install_preview(
        db, extension, organization_id=organization_id
    )


# ── installing ─────────────────────────────────────────────────────────────────────────────────

@router.post(
    "/{slug}/installations",
    response_model=InstallCreateResponse,
    status_code=201,
    summary="Install an add-on into an organization",
    description=(
        "Returns the credential and the signing secret once. They are not readable afterwards."
    ),
)
async def install(
    slug: str,
    request: InstallRequest,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallCreateResponse:
    extension = await extension_service.get_extension_or_404(
        db, slug, caller_email=user.email, is_admin=_is_admin(user)
    )
    vault_client = get_vault_integrations_client()

    record, key_id, key_secret, signing_secret = await extension_install.install_extension(
        db, vault_client,
        extension=extension,
        user=user,
        is_admin=_is_admin(user),
        organization_id=request.organization_id,
        consent_version=request.consent.version,
        consent_digest=request.consent.manifest_digest,
        capabilities=request.capabilities,
        grants=request.cluster_permissions,
        settings_values=request.settings,
    )
    organization = (
        await db.execute(
            select(Organization).where(Organization.id == record.organization_id)
        )
    ).scalar_one_or_none()

    _audit(
        "extension.install", record.id,
        organization_id=record.organization_id,
        organization_slug=organization.slug if organization else None,
        details={
            "extension_slug": slug,
            "api_key_id": key_id,
            "capabilities": list(record.granted_capabilities),
            **issued_credential_audit_fields({"capabilities": list(record.granted_capabilities)}),
            "cluster_ids": [
                g.get("cluster_id") for g in record.granted_scope if isinstance(g, dict)
            ],
        },
    )
    return InstallCreateResponse(
        install=extension_install.to_response(
            record, extension, organization.slug if organization else None
        ),
        api_key_id=key_id,
        api_key_secret=key_secret,
        webhook_signing_secret=signing_secret,
    )


@router.get(
    "/installations",
    response_model=List[InstallResponse],
    summary="List installations you can see",
)
async def list_installations(
    organization_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> List[InstallResponse]:
    allowed = await _allowed_org_ids(user, db)
    query = select(ExtensionInstallation).order_by(ExtensionInstallation.id)
    if allowed is not None:
        query = query.where(ExtensionInstallation.organization_id.in_(allowed or [-1]))
    if organization_id is not None:
        query = query.where(ExtensionInstallation.organization_id == organization_id)
    if status:
        query = query.where(ExtensionInstallation.status == status)

    rows = list((await db.execute(query)).scalars().all())
    out: List[InstallResponse] = []
    for record in rows:
        extension = (
            await db.execute(select(Extension).where(Extension.id == record.extension_id))
        ).scalar_one()
        organization = (
            await db.execute(
                select(Organization).where(Organization.id == record.organization_id)
            )
        ).scalar_one_or_none()
        out.append(
            extension_install.to_response(
                record, extension, organization.slug if organization else None
            )
        )
    return out


@router.get(
    "/installations/{install_id}",
    response_model=InstallResponse,
    summary="Read one installation",
)
async def get_installation(
    install_id: int,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallResponse:
    record, extension, org_slug = await _load(db, install_id, user)
    return extension_install.to_response(
        record, extension, org_slug, credential=_credential_health(record.api_key_id)
    )


def _credential_health(key_id: str) -> Optional[Dict[str, Any]]:
    """When the credential was last used and when it expires, or None if it cannot be read.

    important: only the detail read pays for this. The listing renders many installations and a
    Vault round trip each would make opening the page cost one request per row.

    A failure here must not fail the page. An operator opening an installation because something
    looks wrong is the worst moment to answer 503, and Vault sealing itself is a live condition on
    this deployment. None is returned and the response says the credential was unreadable, which
    is honest and different from "never used".
    """
    try:
        from app.routers.integrations import (
            get_api_key_from_vault,
            get_vault_integrations_client,
        )

        metadata, _ = get_api_key_from_vault(get_vault_integrations_client(), key_id)
    except Exception as exc:
        logger.warning("Could not read credential health for %s: %s", key_id, exc)
        return None
    return {
        "last_used_at": metadata.get("last_used_at"),
        "expires_at": metadata.get("expires_at"),
    }


@router.put(
    "/installations/{install_id}/scope",
    response_model=InstallResponse,
    summary="Change what an installation reaches",
    description="Declarative: state the whole grant, not a delta, so a repeat converges.",
)
async def rescope(
    install_id: int,
    request: ScopeRequest,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallResponse:
    record, extension, org_slug = await _load(db, install_id, user)
    key_change: Dict[str, Any] = {}
    record = await extension_install.rescope_install(
        db, get_vault_integrations_client(),
        install=record,
        extension=extension,
        user=user,
        is_admin=_is_admin(user),
        consent_version=request.consent.version,
        consent_digest=request.consent.manifest_digest,
        capabilities=request.capabilities,
        grants=request.cluster_permissions,
        audit_fields=key_change,
    )
    _audit(
        "extension.install.rescope", install_id,
        organization_id=record.organization_id, organization_slug=org_slug,
        details={
            "capabilities": list(record.granted_capabilities),
            "api_key_id": record.api_key_id,
            **key_change,
            "cluster_ids": [
                g.get("cluster_id") for g in record.granted_scope if isinstance(g, dict)
            ],
        },
    )
    return extension_install.to_response(record, extension, org_slug)


@router.post(
    "/installations/{install_id}/suspend",
    response_model=InstallResponse,
    summary="Stop an installation without removing it",
)
async def suspend(
    install_id: int,
    reason: Optional[str] = Body(None, embed=True),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallResponse:
    record, extension, org_slug = await _load(db, install_id, user)
    if record.status != "suspended":
        record = await extension_install.set_install_status(
            db, install=record, status="suspended", actor=user.email, reason=reason
        )
        _audit(
            "extension.install.suspend", install_id,
            organization_id=record.organization_id, organization_slug=org_slug,
            details={"reason": reason},
        )
    return extension_install.to_response(record, extension, org_slug)


@router.post(
    "/installations/{install_id}/resume",
    response_model=InstallResponse,
    summary="Let a suspended installation work again",
)
async def resume(
    install_id: int,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallResponse:
    record, extension, org_slug = await _load(db, install_id, user)
    if extension.status == "disabled":
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409, detail="This add-on is disabled and cannot be resumed"
        )
    if record.status != "active":
        record = await extension_install.set_install_status(
            db, install=record, status="active", actor=user.email
        )
        _audit(
            "extension.install.resume", install_id,
            organization_id=record.organization_id, organization_slug=org_slug,
        )
    return extension_install.to_response(record, extension, org_slug)


@router.post(
    "/installations/{install_id}/credential/rotate",
    response_model=InstallCreateResponse,
    summary="Issue a new credential for an installation",
    description=(
        "The superseded credential keeps working for the grace window, then stops. Returns the "
        "new credential once."
    ),
)
async def rotate(
    install_id: int,
    grace_seconds: int = Body(0, embed=True),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallCreateResponse:
    record, extension, org_slug = await _load(db, install_id, user)
    vault_client = get_vault_integrations_client()
    record, key_id, key_secret = await extension_install.rotate_credential(
        db, vault_client,
        install=record, extension=extension, organization_slug=org_slug,
        actor=user.email, grace_seconds=grace_seconds,
    )
    from app.services.extension_credentials import rotate_signing_secret

    signing_secret = rotate_signing_secret(vault_client, record.id)
    _audit(
        "extension.install.credential.rotate", install_id,
        organization_id=record.organization_id, organization_slug=org_slug,
        details={
            "api_key_id": key_id,
            "grace_seconds": grace_seconds,
            "capabilities": list(record.granted_capabilities or []),
            **issued_credential_audit_fields({"capabilities": list(record.granted_capabilities or [])}),
        },
    )
    return InstallCreateResponse(
        install=extension_install.to_response(record, extension, org_slug),
        api_key_id=key_id,
        api_key_secret=key_secret,
        webhook_signing_secret=signing_secret,
    )


@router.put(
    "/installations/{install_id}/settings",
    response_model=InstallResponse,
    summary="Configure an installation",
    description=(
        "Secret values are stored write-only. Omitting one leaves the stored value alone, so "
        "saving another field does not blank a credential nobody retyped."
    ),
)
async def update_settings(
    install_id: int,
    request: SettingsUpdate,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> InstallResponse:
    record, extension, org_slug = await _load(db, install_id, user)
    record = await extension_install.update_settings(
        db, get_vault_integrations_client(),
        install=record, extension=extension, values=request.values,
    )
    _audit(
        "extension.install.settings", install_id,
        organization_id=record.organization_id, organization_slug=org_slug,
        # Names only. The values are the operator's, and one of them is a credential.
        details={"fields": sorted(request.values)},
    )
    return extension_install.to_response(record, extension, org_slug)


@router.delete(
    "/installations/{install_id}",
    status_code=204,
    summary="Uninstall",
    description="Removes the credential, the signing secret, the stored settings and the record.",
)
async def uninstall(
    install_id: int,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> None:
    record, extension, org_slug = await _load(db, install_id, user)
    organization_id, api_key_id = record.organization_id, record.api_key_id
    try:
        removed = await extension_install.uninstall(
            db, get_vault_integrations_client(), install=record, user=user
        )
    except KeyOwnsOrganizations as refusal:
        _audit(
            "extension.install.uninstall", install_id,
            organization_id=organization_id, organization_slug=org_slug,
            details={"extension_slug": extension.slug, "api_key_id": api_key_id, **refusal.audit_details},
            result="denied", error_code="key_owns_organizations",
        )
        raise
    _audit(
        "extension.install.uninstall", install_id,
        organization_id=organization_id, organization_slug=org_slug,
        details={
            "extension_slug": extension.slug,
            "api_key_id": api_key_id,
            "artefacts_removed": removed,
        },
    )
