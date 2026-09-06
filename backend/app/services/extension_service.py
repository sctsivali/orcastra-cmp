"""Publishing, updating and withdrawing an add-on definition.

Nothing here authorizes access to anything. An `Extension` row is a description that an operator
later consents to; the installation is what grants. Keeping that separation explicit is what stops
"the author updated their manifest" from becoming "the add-on gained a capability".
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.extension_manifest import canonical_manifest_json, manifest_digest
from app.models.extension_models import Extension, ExtensionInstallation
from app.models.extension_schemas import ExtensionManifest, ExtensionResponse, ExtensionUpdate

logger = logging.getLogger(__name__)


def _not_found() -> HTTPException:
    """404 rather than 403 for something the caller may not see.

    The same shape `_authorize_api_key` uses, and for the same reason: an id outside the caller's
    reach must not be distinguishable from one that does not exist, or the refusal itself becomes
    an enumeration oracle.
    """
    return HTTPException(status_code=404, detail="Extension not found")


def to_response(extension: Extension) -> ExtensionResponse:
    return ExtensionResponse(
        id=extension.id,
        slug=extension.slug,
        name=extension.name,
        publisher=extension.publisher,
        version=extension.version,
        status=extension.status,
        description=extension.description,
        homepage_url=extension.homepage_url,
        support_url=extension.support_url,
        owner_email=extension.owner_email,
        requested_capabilities=list(extension.requested_capabilities or []),
        requested_scope=dict(extension.requested_scope or {}),
        settings_schema=list(extension.settings_schema or []) or None,
        default_webhook_url=extension.default_webhook_url,
        default_webhook_events=list(extension.default_webhook_events or []) or None,
        manifest_digest=extension.manifest_digest,
        created_at=extension.created_at,
        updated_at=extension.updated_at,
    )


async def create_extension(
    db: AsyncSession,
    *,
    manifest: ExtensionManifest,
    owner_email: str,
    publisher_organization_id: Optional[int],
) -> Extension:
    existing = await db.execute(select(Extension).where(Extension.slug == manifest.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409, detail=f"The slug '{manifest.slug}' is already taken"
        )

    document = manifest.model_dump(mode="json", exclude_none=True)
    extension = Extension(
        slug=manifest.slug,
        name=manifest.name,
        publisher=manifest.publisher,
        description=manifest.description,
        homepage_url=manifest.homepage_url,
        support_url=manifest.support_url,
        version=manifest.version,
        status="draft",
        owner_email=owner_email,
        publisher_organization_id=publisher_organization_id,
        requested_capabilities=list(manifest.capabilities),
        requested_scope=manifest.scope.model_dump(),
        settings_schema=[field.model_dump(exclude_none=True) for field in manifest.settings] or None,
        default_webhook_url=manifest.webhook_url,
        default_webhook_events=manifest.webhook_events,
        manifest=document,
        manifest_digest=manifest_digest(document),
    )
    db.add(extension)
    await db.commit()
    await db.refresh(extension)
    logger.info("Registered extension %s v%s", extension.slug, extension.version)
    return extension


def _semver_tuple(version: str) -> tuple:
    """Compare on the numeric core only. A pre-release suffix does not order a version here.

    note: `VERSION_RE` permits a `-rc.1` style suffix, and comparing those correctly means
    implementing the whole semver precedence rule. Rather than half-implement it, the core is what
    orders and a suffix-only change is refused as not-greater, which is the safe direction: it
    refuses an ambiguous bump instead of accepting one nobody can reason about.
    """
    core = version.split("-")[0].split("+")[0]
    return tuple(int(part) for part in core.split("."))


async def publish_version(
    db: AsyncSession, extension: Extension, *, manifest: ExtensionManifest, actor_email: str
) -> Extension:
    """Replace what an add-on asks for, under a strictly greater version.

    Existing installations are deliberately untouched. Their grants live on their own row, so they
    keep working at the version they consented to, and their stored digest no longer matches this
    one, which makes any rescope answer 409 until an operator approves the new manifest. That is
    the re-consent the documentation already promises.
    """
    if manifest.slug != extension.slug:
        raise HTTPException(
            status_code=422,
            detail="A new version keeps the same slug. Publishing under a different slug is a "
            "different add-on.",
        )
    if extension.owner_email != actor_email:
        raise HTTPException(status_code=404, detail="Extension not found")
    if _semver_tuple(manifest.version) <= _semver_tuple(extension.version):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Version {manifest.version} is not greater than the published "
                f"{extension.version}. A new version moves forward."
            ),
        )

    document = manifest.model_dump(mode="json", exclude_none=True)
    extension.name = manifest.name
    extension.publisher = manifest.publisher
    extension.description = manifest.description
    extension.homepage_url = manifest.homepage_url
    extension.support_url = manifest.support_url
    extension.version = manifest.version
    extension.requested_capabilities = list(manifest.capabilities)
    extension.requested_scope = manifest.scope.model_dump()
    extension.settings_schema = [
        field.model_dump(exclude_none=True) for field in manifest.settings
    ] or None
    extension.default_webhook_url = manifest.webhook_url
    extension.default_webhook_events = manifest.webhook_events
    extension.manifest = document
    extension.manifest_digest = manifest_digest(document)
    await db.commit()
    await db.refresh(extension)
    logger.info(
        "Published %s v%s; existing installations stay on what they consented to",
        extension.slug,
        extension.version,
    )
    return extension


async def get_extension_or_404(
    db: AsyncSession, slug: str, *, caller_email: str, is_admin: bool
) -> Extension:
    """A draft is visible to its owner and to an admin. Everyone else gets 404."""
    result = await db.execute(select(Extension).where(Extension.slug == slug))
    extension = result.scalar_one_or_none()
    if extension is None:
        raise _not_found()
    if extension.status == "draft" and not is_admin and extension.owner_email != caller_email:
        raise _not_found()
    return extension


async def assert_owned(extension: Extension, *, caller_email: str, is_admin: bool) -> None:
    if not is_admin and extension.owner_email != caller_email:
        raise _not_found()


async def list_extensions(
    db: AsyncSession,
    *,
    caller_email: str,
    is_admin: bool,
    status: Optional[str] = None,
    mine: bool = False,
) -> List[Extension]:
    query = select(Extension).order_by(Extension.name)
    if status:
        query = query.where(Extension.status == status)
    if mine:
        query = query.where(Extension.owner_email == caller_email)
    result = await db.execute(query)
    rows = list(result.scalars().all())
    if is_admin:
        return rows
    # A draft belongs to whoever is still working on it.
    return [e for e in rows if e.status != "draft" or e.owner_email == caller_email]


async def update_extension(
    db: AsyncSession, extension: Extension, update: ExtensionUpdate
) -> Extension:
    """Presentation may change. What the add-on asks for may not.

    important: the slug is immutable and the version is not editable here. A consent binds to a
    version and a digest, so moving either would leave every installation claiming agreement to a
    document that no longer exists. Changing the ask is `publish_version`, which is a deliberate
    act with its own re-consent consequence rather than a side effect of fixing a typo.
    """
    document: Dict[str, Any] = dict(extension.manifest or {})
    for field in ("name", "description", "homepage_url", "support_url"):
        value = getattr(update, field)
        if value is not None:
            setattr(extension, field, value)
            document[field] = value
    if update.status is not None:
        extension.status = update.status

    extension.manifest = document
    # important: the digest deliberately does NOT move here. It used to be recomputed on every
    # edit, so correcting a typo in a description silently invalidated the consent of every
    # existing installation and answered 409 on their next rescope. Nothing on this path changes
    # what the add-on asks for, and a digest is a promise about that, not about the prose. Only
    # `publish_version` may move it, because only a version bump changes the ask.
    await db.commit()
    await db.refresh(extension)
    return extension


async def delete_extension(db: AsyncSession, extension: Extension) -> None:
    """Refuse while anything is installed.

    The application-layer twin of ON DELETE RESTRICT. Either alone leaves the other as the only
    thing standing, and the database's refusal surfaces as a 500 rather than an explanation.
    """
    count = await db.execute(
        select(func.count())
        .select_from(ExtensionInstallation)
        .where(ExtensionInstallation.extension_id == extension.id)
    )
    live = count.scalar_one()
    if live:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{live} organization(s) still have this add-on installed. "
                "Uninstall it from each of them first."
            ),
        )
    await db.delete(extension)
    await db.commit()


def manifest_matches(extension: Extension, digest: str) -> bool:
    """Whether the operator consented to the document that is stored now.

    Recomputed rather than compared to the stored column, so a digest written by an older code
    path cannot be what a consent is checked against.
    """
    return manifest_digest(extension.manifest or {}) == digest


def canonical_document(extension: Extension) -> str:
    return canonical_manifest_json(extension.manifest or {})
