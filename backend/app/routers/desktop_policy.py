"""Whether an organization permits an agent to see and drive its guests' desktops.

`desktop_control_policy` defaults to refusing; these routes are how an organization gives or
withdraws its consent.

The decision belongs to the organization that owns the cluster rather than to whoever issued the
API key: screen contents sent to a third-party model are a disclosure to a sub-processor, and the
two are consented to by different people.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_tenant
from app.core.rbac import get_highest_role, resolve_roles
from app.services.activity_logger import get_activity_logger
from app.services.desktop_policy import DesktopConsent, get_consent, set_consent
from app.services.organization_service import OrganizationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organizations", tags=["Desktop Control"])

activity_logger = get_activity_logger()


class DesktopControlPolicyResponse(BaseModel):
    organization_id: int
    enabled: bool = Field(description="Whether an agent may capture this organization's screens")
    allow_input: bool = Field(description="Whether an agent may also type and click")
    allow_clipboard: bool = Field(
        default=False, description="Whether an agent may read this organization's clipboards"
    )
    allow_guest_access: bool = Field(
        default=False,
        description="Whether an add-on install may take instance.exec or instance.files here",
    )


class DesktopControlPolicyRequest(BaseModel):
    enabled: bool = Field(description="Allow an agent to capture this organization's screens")
    allow_input: bool = Field(
        default=False,
        description=(
            "Also allow it to type and click. Ignored unless enabled is true: turning the feature "
            "off clears this rather than leaving it set for the next time it is turned on."
        ),
    )
    allow_clipboard: bool = Field(
        default=False,
        description=(
            "Also allow it to read the clipboard. Its own switch because watching a screen and "
            "lifting whatever was last copied on it are different disclosures."
        ),
    )
    allow_guest_access: bool = Field(
        default=False,
        description=(
            "Allow an add-on installed here to run commands and read files inside guests. This "
            "is root in the container by default, and it used to ride on the screen-capture "
            "switch, which is not what that switch says."
        ),
    )


def _as_response(org_id: int, consent: DesktopConsent) -> DesktopControlPolicyResponse:
    """One builder for both handlers.

    important: set every field here. The response model has defaults, so a handler that forgets a
    field reports the consent as off while the row says it is on.
    """
    return DesktopControlPolicyResponse(
        organization_id=org_id,
        enabled=consent.enabled,
        allow_input=consent.allow_input,
        allow_clipboard=consent.allow_clipboard,
        allow_guest_access=consent.allow_guest_access,
    )


async def _assert_member(
    org_id: int, user: AuthenticatedUser, db: AsyncSession
) -> None:
    """Allow any member of the organization, and an admin acting for it.

    important: membership is the boundary, whatever the role tier. A Tenant member must be able to
    give or withdraw its organization's consent; requiring Partner would leave the Admin, who also
    issues the key grant, holding both halves of the control.

    A non-member gets 404, as on the rest of the org surface, so a stranger cannot learn whether
    the organization exists.
    """
    role = get_highest_role(resolve_roles(user)).value
    service = OrganizationService(db)
    if not await service.user_has_access(org_id, user.email, role):
        raise HTTPException(status_code=404, detail="Organization not found")


@router.get(
    "/{org_id}/desktop-control",
    response_model=DesktopControlPolicyResponse,
    summary="Read this organization's desktop-control consent",
)
async def read_policy(
    org_id: int,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
) -> DesktopControlPolicyResponse:
    await _assert_member(org_id, user, db)
    consent = await get_consent(db, org_id)
    return _as_response(org_id, consent)


@router.put(
    "/{org_id}/desktop-control",
    response_model=DesktopControlPolicyResponse,
    summary="Set this organization's desktop-control consent",
    dependencies=[Depends(check_read_only_mode)],
)
async def set_policy(
    org_id: int,
    body: DesktopControlPolicyRequest,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
) -> DesktopControlPolicyResponse:
    await _assert_member(org_id, user, db)
    consent = await set_consent(
        db,
        org_id,
        enabled=body.enabled,
        allow_input=body.allow_input,
        allow_clipboard=body.allow_clipboard,
        allow_guest_access=body.allow_guest_access,
        updated_by=user.email,
    )
    # Consent to a third-party disclosure must be attributable, including when it is withdrawn.
    activity_logger.log_activity(
        action="desktop_control.consent.set",
        target_type="organization",
        target_id=str(org_id),
        target_organization_id=org_id,
        result="success",
        details={
            "enabled": consent.enabled,
            "allow_input": consent.allow_input,
            "allow_clipboard": consent.allow_clipboard,
            "allow_guest_access": consent.allow_guest_access,
        },
    )
    return _as_response(org_id, consent)
