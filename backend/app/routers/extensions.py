"""Publishing an add-on. Partner-authored, JWT-authenticated.

Nothing on this router grants access to anything. It manages descriptions that an operator later
consents to; `extension_installs.py` is where a grant is actually made.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capabilities import CAPABILITIES
from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_partner
from app.core.extension_manifest import EXTENSION_EVENTS, MANIFEST_MAX_BYTES, SETTING_TYPES
from app.core.org_scope import get_user_org_ids, org_scope_dependency
from app.core.rbac import Role
from app.models.extension_schemas import (
    ExtensionManifest,
    ExtensionResponse,
    ExtensionUpdate,
    capability_request_info,
)
from app.services import extension_service
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/extensions",
    tags=["Extensions"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)


def _is_admin(user: AuthenticatedUser) -> bool:
    return Role.ADMIN.value in (user.roles or [])


def _audit(action: str, slug: str, details: Optional[Dict[str, Any]] = None) -> None:
    try:
        activity_logger.log_activity(
            action=action, target_type="extension", target_id=slug, details=details or {}
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the request
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


@router.get(
    "/vocabulary",
    summary="What an add-on may declare",
    description=(
        "Capabilities, events and settings field types, served rather than published as a "
        "constant so a manifest cannot name something this deployment does not enforce."
    ),
)
async def get_vocabulary() -> Dict[str, Any]:
    """Served, not hardcoded.

    The same rule the capability picker already follows: a client that hardcodes this list will
    eventually offer something the server refuses, and the operator sees a form that lies.
    """
    return {
        "capabilities": [
            capability_request_info(name, description).model_dump()
            for name, description in sorted(CAPABILITIES.items())
        ],
        "events": [
            {"name": name, "description": description}
            for name, description in sorted(EXTENSION_EVENTS.items())
        ],
        "setting_types": [
            {"name": name, "description": description}
            for name, description in sorted(SETTING_TYPES.items())
        ],
    }


async def assert_manifest_within_limit(request: Request) -> None:
    """Hold an API publisher to the same 16 KB the dialog and the docs already promise.

    The cap was documented as a server rule and enforced only in the browser, so publishing the
    same manifest through the API succeeded where the UI refused it. Reading the cached body here
    costs nothing: FastAPI has already read it to build the model.
    """
    body = await request.body()
    if len(body) > MANIFEST_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That manifest is {len(body)} bytes. The limit is {MANIFEST_MAX_BYTES}."
            ),
        )


@router.post(
    "",
    response_model=ExtensionResponse,
    status_code=201,
    summary="Publish an add-on",
    description="Register a manifest. The add-on starts as a draft, visible only to its owner.",
)
async def register_extension(
    manifest: ExtensionManifest,
    _: None = Depends(check_read_only_mode),
    __: None = Depends(assert_manifest_within_limit),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> ExtensionResponse:
    org_ids = await get_user_org_ids(user, db)
    extension = await extension_service.create_extension(
        db,
        manifest=manifest,
        owner_email=user.email,
        publisher_organization_id=org_ids[0] if org_ids else None,
    )
    _audit(
        "extension.register",
        extension.slug,
        {"version": extension.version, "capabilities": list(extension.requested_capabilities)},
    )
    return extension_service.to_response(extension)


@router.get(
    "",
    response_model=List[ExtensionResponse],
    summary="List add-ons",
)
async def list_extensions(
    status: Optional[str] = Query(None),
    mine: bool = Query(False),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> List[ExtensionResponse]:
    rows = await extension_service.list_extensions(
        db, caller_email=user.email, is_admin=_is_admin(user), status=status, mine=mine
    )
    return [extension_service.to_response(row) for row in rows]


@router.get(
    "/{slug}",
    response_model=ExtensionResponse,
    summary="Read one add-on",
)
async def get_extension(
    slug: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> ExtensionResponse:
    extension = await extension_service.get_extension_or_404(
        db, slug, caller_email=user.email, is_admin=_is_admin(user)
    )
    return extension_service.to_response(extension)


@router.patch(
    "/{slug}",
    response_model=ExtensionResponse,
    summary="Update an add-on",
    description=(
        "The slug and the version cannot change here. A consent binds to both, so moving either "
        "would leave every installation claiming agreement to a document that no longer exists."
    ),
)
async def update_extension(
    slug: str,
    update: ExtensionUpdate,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> ExtensionResponse:
    extension = await extension_service.get_extension_or_404(
        db, slug, caller_email=user.email, is_admin=_is_admin(user)
    )
    await extension_service.assert_owned(
        extension, caller_email=user.email, is_admin=_is_admin(user)
    )
    extension = await extension_service.update_extension(db, extension, update)
    _audit("extension.update", slug, {"status": extension.status})
    return extension_service.to_response(extension)


@router.post(
    "/{slug}/versions",
    response_model=ExtensionResponse,
    summary="Publish a new version of an add-on",
    description=(
        "Send the whole manifest, not a patch, under a version greater than the published one. "
        "Existing installations are untouched: they keep the reach they were granted and stay on "
        "the version they consented to. Their stored digest no longer matches, so changing one of "
        "them requires an operator to approve the new manifest first."
    ),
    status_code=200,
)
async def publish_extension_version(
    slug: str,
    manifest: ExtensionManifest,
    _: None = Depends(check_read_only_mode),
    __: None = Depends(assert_manifest_within_limit),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> ExtensionResponse:
    extension = await extension_service.get_extension_or_404(
        db, slug, caller_email=user.email, is_admin=_is_admin(user)
    )
    previous = extension.version
    extension = await extension_service.publish_version(
        db, extension, manifest=manifest, actor_email=user.email
    )
    _audit(
        "extension.version.publish",
        slug,
        {
            "from_version": previous,
            "to_version": extension.version,
            "capabilities": list(extension.requested_capabilities or []),
        },
    )
    return extension_service.to_response(extension)


@router.delete(
    "/{slug}",
    status_code=204,
    summary="Withdraw an add-on",
    description="Refused while any organization still has it installed.",
)
async def delete_extension(
    slug: str,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> None:
    extension = await extension_service.get_extension_or_404(
        db, slug, caller_email=user.email, is_admin=_is_admin(user)
    )
    await extension_service.assert_owned(
        extension, caller_email=user.email, is_admin=_is_admin(user)
    )
    await extension_service.delete_extension(db, extension)
    _audit("extension.delete", slug)
