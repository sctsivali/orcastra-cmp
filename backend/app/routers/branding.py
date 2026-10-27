"""The deployment's default branding: the sign-in page, the tab title and icon, and every account
without branding of its own (an Administrator, an organization with no row and no provider).

Administrator only. Organizations keep their own rows (routers/organization_branding.py).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_admin
from app.services.branding import store
from app.services.branding.validation import BrandingInvalid

router = APIRouter(
    prefix="/branding",
    tags=["Branding"],
    dependencies=[Depends(require_admin())],
)


class DeploymentBrandingBody(BaseModel):
    settings: dict = Field(default_factory=dict)


class DeploymentBrandingResponse(BaseModel):
    # {} until an Administrator sets it: the built-in look applies.
    settings: dict
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    version: str


async def _response(db: AsyncSession) -> DeploymentBrandingResponse:
    row = await store.deployment_row(db)
    resolved = await store.deployment_branding(db, with_settings=False)
    return DeploymentBrandingResponse(
        settings=row.settings if row else {},
        updated_by=row.updated_by if row else None,
        updated_at=store.stamp(row.updated_at) if row else None,
        version=resolved.version,
    )


@router.get("/deployment", response_model=DeploymentBrandingResponse)
async def get_deployment_branding(response: Response, db: AsyncSession = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store, private"
    return await _response(db)


@router.put("/deployment", response_model=DeploymentBrandingResponse)
async def update_deployment_branding(
    body: DeploymentBrandingBody,
    response: Response,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
):
    response.headers["Cache-Control"] = "no-store, private"
    try:
        await store.save_deployment_branding(db, body.settings, user.email or "")
    except BrandingInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _response(db)


@router.delete("/deployment", status_code=status.HTTP_204_NO_CONTENT)
async def reset_deployment_branding(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
):
    await store.reset_deployment_branding(db)
