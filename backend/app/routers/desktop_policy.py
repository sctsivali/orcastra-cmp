"""Whether an organization permits an agent to see and drive its guests' desktops.

important: this router exists because the gate that reads this row shipped before anything could
write it. `desktop_control_policy` defaults to refusing, so without a way to set it the feature is
correctly but permanently off, and an operator has no way to tell a deliberate refusal from a
missing endpoint. A consent nobody can give is not a consent.

The decision belongs to the organization that owns the cluster rather than to whoever issued the
API key: screen contents leaving for a third-party model is a disclosure to a sub-processor, not
an ordinary API call, and the two are consented to by different people.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_tenant
from app.core.rbac import get_highest_role, resolve_roles
from app.services.activity_logger import get_activity_logger
from app.services.desktop_policy import get_consent, set_consent
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


async def _assert_member(
    org_id: int, user: AuthenticatedUser, db: AsyncSession
) -> None:
    """The organization's own people decide this, and an admin acting for them.

    important: any member, including a Tenant. These routes required Partner or above, which meant
    the organization whose screens are at stake could not give or withdraw its own consent unless
    it happened to hold Partner. Since an Admin is the only role that can also issue the key
    grant, both halves of a control this module describes as belonging to two different people
    collapsed into one actor. Membership is the boundary here; the tier is not.

    note: 404 rather than 403 for a non-member, matching the rest of the org surface. Whether an
    organization exists is not something a stranger should learn from the difference.
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
    return DesktopControlPolicyResponse(
        organization_id=org_id, enabled=consent.enabled, allow_input=consent.allow_input
    )


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
    # Consent to a third-party disclosure is exactly the kind of decision that has to be
    # attributable afterwards, including when it is withdrawn.
    activity_logger.log_activity(
        action="desktop_control.consent.set",
        target_type="organization",
        target_id=str(org_id),
        target_organization_id=org_id,
        result="success",
        details={"enabled": consent.enabled, "allow_input": consent.allow_input},
    )
    return DesktopControlPolicyResponse(
        organization_id=org_id, enabled=consent.enabled, allow_input=consent.allow_input
    )
