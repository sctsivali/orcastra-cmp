"""The deployment default branding, before sign-in: the sign-in page, the tab title and the favicon.

No authentication, so the answer is an allow-list of display fields and nothing that names a
person or an organization. An uploaded image is not inlined: it is served from /assets/, decoded,
under its own type and a policy that runs nothing inside it, which is what makes an SVG logo safe
to accept (validation.py). A URL the Administrator gave (Cloudinary, or a path on this site) is
passed through as it is.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.services.branding import store
from app.services.branding.validation import BrandingInvalid, decode_data_uri

router = APIRouter(prefix="/public/branding", tags=["Branding"])

ASSETS = {
    "logo-light": ("logo", "lightModeLogo"),
    "logo-dark": ("logo", "darkModeLogo"),
    "favicon": ("logo", "favicon"),
}
_SANDBOX = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


class PublicBranding(BaseModel):
    app_name: Optional[str] = None
    tagline: Optional[str] = None
    footer_text: Optional[str] = None
    accent_color: Optional[str] = None
    theme: Optional[str] = None
    logo_light: Optional[str] = None
    logo_dark: Optional[str] = None
    favicon: Optional[str] = None
    version: str


def _asset_url(name: str, value: Optional[str], version: str) -> Optional[str]:
    if not value:
        return None
    if value.startswith("data:"):
        return f"{get_settings().api_prefix}/public/branding/assets/{name}?v={version}"
    return value


@router.get("", response_model=PublicBranding)
async def get_public_branding(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    resolved = await store.deployment_branding(db)
    etag = f'"{resolved.version}"'
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["ETag"] = etag
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
    settings = resolved.settings
    branding = settings.get("branding", {})
    logo = settings.get("logo", {})
    colors = settings.get("colors", {})
    return PublicBranding(
        app_name=branding.get("appName"),
        tagline=branding.get("tagline"),
        footer_text=branding.get("footerText"),
        accent_color=colors.get("accentColor"),
        theme=settings.get("theme"),
        logo_light=_asset_url("logo-light", logo.get("lightModeLogo"), resolved.version),
        logo_dark=_asset_url("logo-dark", logo.get("darkModeLogo"), resolved.version),
        favicon=_asset_url("favicon", logo.get("favicon"), resolved.version),
        version=resolved.version,
    )


@router.get("/assets/{name}")
async def get_public_branding_asset(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    if name not in ASSETS:
        raise HTTPException(status_code=404, detail="Not found")
    resolved = await store.deployment_branding(db)
    section, key = ASSETS[name]
    value = resolved.settings.get(section, {}).get(key)
    if not isinstance(value, str) or not value.startswith("data:"):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        content_type, data = decode_data_uri(value)
    except BrandingInvalid as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    current = request.query_params.get("v") == resolved.version
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable" if current else "public, max-age=60",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": _SANDBOX,
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Disposition": "inline",
        },
    )
