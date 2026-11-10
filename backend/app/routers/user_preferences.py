"""User preferences: each user's theme (light, dark or system), and the branding editor's image proxy.

Branding is not per user. It belongs to an organization, its provider or the deployment
(services/branding/), and a personal row keeps only the theme. Rows are keyed by the OIDC subject
of the validated token.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import logging
import base64

from app.core.database import get_db
from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.core.dependencies import check_read_only_mode, require_authenticated, AuthenticatedUser
from app.models.db_models import UserPreferences

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/user-preferences",
    tags=["User Preferences"],
    dependencies=[Depends(require_authenticated())],
)


# ============================================
# Pydantic Models
# ============================================

class LogoSettings(BaseModel):
    """Logo settings for different themes."""
    lightModeLogo: Optional[str] = None
    darkModeLogo: Optional[str] = None
    favicon: Optional[str] = None


class BackgroundSettings(BaseModel):
    """Background settings for landing page."""
    backgroundImage: Optional[str] = None
    backgroundOverlayOpacity: int = 85
    backgroundBlur: int = 0


class BrandingSettings(BaseModel):
    """Branding text settings."""
    appName: str = "Orcastra"
    tagline: str = "Enterprise operations center for managing virtualized infrastructure and containers across distributed clusters."
    footerText: str = "© {YEAR} ORCASTRA"


# important: the product defaults. They must match frontend/src/types/personalization.ts, the
# CSS seeds in frontend/src/app/globals.css and get_default_settings() below. A user with no
# stored row receives these, so drift means the API hands out one colour while the stylesheet
# paints another. scripts/checks/check-personalization-defaults.mjs fails when the copies differ.
DEFAULT_ACCENT_COLOR = "#8467e4"
DEFAULT_SIDEBAR_BG_COLOR = "#321f6f"
DEFAULT_SIDEBAR_TEXT_COLOR = "#f8fafc"


class ColorSettings(BaseModel):
    """Color/theme settings."""
    accentColor: str = DEFAULT_ACCENT_COLOR
    sidebarBgColor: str = DEFAULT_SIDEBAR_BG_COLOR
    sidebarTextColor: str = DEFAULT_SIDEBAR_TEXT_COLOR
    sidebarLogoColor: str = "auto"
    useCustomAccent: bool = False
    useCustomSidebarBg: bool = True
    useCustomSidebarText: bool = False
    autoTextColor: bool = True


class PersonalizationSettingsRequest(BaseModel):
    """Request model for updating personalization settings."""
    logo: Optional[LogoSettings] = None
    background: Optional[BackgroundSettings] = None
    branding: Optional[BrandingSettings] = None
    colors: Optional[ColorSettings] = None


class PersonalizationSettingsResponse(BaseModel):
    """Response model for personalization settings."""
    user_id: str
    user_email: Optional[str] = None
    settings: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DefaultPersonalizationResponse(BaseModel):
    """Default personalization settings when user has no saved settings."""
    logo: LogoSettings = Field(default_factory=LogoSettings)
    background: BackgroundSettings = Field(default_factory=BackgroundSettings)
    branding: BrandingSettings = Field(default_factory=BrandingSettings)
    colors: ColorSettings = Field(default_factory=ColorSettings)
    lastModified: Optional[str] = None


# ============================================
# Helper Functions
# ============================================

def get_default_settings() -> Dict[str, Any]:
    """Return default personalization settings."""
    return {
        "logo": {
            "lightModeLogo": None,
            "darkModeLogo": None,
            "favicon": None,
        },
        "background": {
            "backgroundImage": None,
            "backgroundOverlayOpacity": 85,
            "backgroundBlur": 0,
        },
        "branding": {
            "appName": "Orcastra",
            "tagline": "Enterprise operations center for managing virtualized infrastructure and containers across distributed clusters. Streamline resource management with real-time monitoring and automation.",
            "footerText": "© {YEAR} ORCASTRA",
        },
        "colors": {
            "accentColor": DEFAULT_ACCENT_COLOR,
            "sidebarBgColor": DEFAULT_SIDEBAR_BG_COLOR,
            "sidebarTextColor": DEFAULT_SIDEBAR_TEXT_COLOR,
            "sidebarLogoColor": "auto",
            "useCustomAccent": False,
            "useCustomSidebarBg": True,
            "useCustomSidebarText": False,
            "autoTextColor": True,
        },
        "theme": "system",
        "lastModified": None,
    }


THEMES = ("light", "dark", "system")


def _personal_view(user_prefs: UserPreferences) -> Dict[str, Any]:
    """The defaults with this user's theme laid over them. lastModified marks that a theme was chosen."""
    settings = get_default_settings()
    saved = user_prefs.settings or {}
    if saved.get("theme") in THEMES:
        settings["theme"] = saved["theme"]
        settings["lastModified"] = saved.get("lastModified") or user_prefs.updated_at.isoformat()
    return settings


# important: identity comes from the validated JWT principal, never from a request
# value; otherwise any caller could read, overwrite or delete another user's
# preferences.
def current_identity(user: AuthenticatedUser) -> tuple[str, Optional[str]]:
    if not user or not user.user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user.user_id, user.email


# ============================================
# API Endpoints
# ============================================

@router.get(
    "",
    response_model=Dict[str, Any],
    summary="Get user preferences",
    description="Retrieve personalization settings for the authenticated user. Returns defaults if no preferences are saved."
)
async def get_user_preferences(
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_authenticated()),
) -> Dict[str, Any]:
    """The source of truth for the user's preferences; the frontend reads it at sign-in."""
    
    # No browser or proxy caching: every read comes from the database.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    x_user_id, x_user_email = current_identity(user)
    
    logger.info(f"Fetching preferences for user {x_user_id}")
    
    try:
        result = await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == x_user_id)
        )
        user_prefs = result.scalar_one_or_none()
        
        if user_prefs:
            settings = _personal_view(user_prefs)
            logger.info(f"Returning saved preferences for user {x_user_id}, lastModified: {settings['lastModified']}")
            return settings
        else:
            logger.info(f"No preferences found for user {x_user_id}, returning defaults")
            return get_default_settings()
            
    except Exception as e:
        logger.error(f"Error fetching user preferences for {x_user_id}: {e}")
        # Return defaults on error to avoid breaking the UI
        return get_default_settings()


@router.put(
    "",
    response_model=Dict[str, Any],
    summary="Set or clear the user's theme",
    description="Stores the authenticated user's theme (light, dark or system); null clears it. Branding is per organization.",
)
async def update_user_preferences(
    settings: Dict[str, Any],
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_authenticated()),
    _: None = Depends(check_read_only_mode),
) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    x_user_id, x_user_email = current_identity(user)
    # important: only the theme is kept. Branding belongs to an organization or to the deployment
    # (services/branding/); a personal copy would be handed to everyone in the owner's
    # organization. `theme: null` clears the choice, so the organization's default theme applies.
    # Other keys a client sends are ignored; keys already stored stay in place for a rollback.
    if "theme" not in settings:
        raise HTTPException(status_code=422, detail="Only the theme is stored per user; send {\"theme\": ...}.")
    theme = settings.get("theme")
    if theme is not None and theme not in THEMES:
        raise HTTPException(status_code=422, detail=f"theme must be one of {', '.join(THEMES)}, or null")

    try:
        result = await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == x_user_id)
        )
        user_prefs = result.scalar_one_or_none()
        stored = dict(user_prefs.settings or {}) if user_prefs else {}
        if theme is None:
            stored.pop("theme", None)
            stored.pop("lastModified", None)
        else:
            stored["theme"] = theme
            stored["lastModified"] = datetime.now(timezone.utc).isoformat()

        if user_prefs and not stored:
            await db.delete(user_prefs)
            await db.commit()
            return get_default_settings()
        if user_prefs:
            user_prefs.settings = stored
            user_prefs.user_email = x_user_email
        elif stored:
            user_prefs = UserPreferences(user_id=x_user_id, user_email=x_user_email, settings=stored)
            db.add(user_prefs)
        else:
            return get_default_settings()
        await db.commit()
        await db.refresh(user_prefs)
        return _personal_view(user_prefs)
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error saving user preferences for {x_user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not save preferences")


@router.delete(
    "",
    summary="Reset user preferences",
    description="Reset personalization settings to defaults for the authenticated user. Removes all customizations."
)
async def reset_user_preferences(
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_authenticated()),
    _: None = Depends(check_read_only_mode),
) -> Dict[str, str]:
    """Delete the user's stored preferences, so the defaults apply."""
    
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    
    x_user_id, x_user_email = current_identity(user)
    
    logger.info(f"Resetting preferences for user {x_user_id}")
    
    try:
        result = await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == x_user_id)
        )
        user_prefs = result.scalar_one_or_none()
        
        if user_prefs:
            await db.delete(user_prefs)
            await db.commit()
            logger.info(f"Successfully deleted preferences for user {x_user_id}")
            return {"message": "Preferences reset to defaults", "status": "success"}
        else:
            logger.info(f"No preferences found to reset for user {x_user_id}")
            return {"message": "No preferences to reset", "status": "success"}
            
    except Exception as e:
        await db.rollback()
        logger.error(f"Error resetting user preferences for {x_user_id}: {e}")
        raise HTTPException(status_code=500, detail="Could not reset preferences")


# ============================================
# Image Proxy - fetch cross-origin images server-side
# ============================================

# important: no SSRF filter of its own. This proxy uses the guarded client (core/safe_http.py,
# core/url_safety.py) like every other outbound fetch, so there is one filter to keep current.


class ProxyImageRequest(BaseModel):
    """Request to proxy-fetch an image from a URL."""
    url: str = Field(..., description="URL of the image to fetch")

class ProxyImageResponse(BaseModel):
    """Base64-encoded image data."""
    data: str = Field(..., description="Base64 data URI (data:image/...;base64,...)")


@router.post(
    "/proxy-image",
    response_model=ProxyImageResponse,
    summary="Fetch image from URL (server-side CORS proxy)",
    description="Fetches an image from a cross-origin URL server-side and returns it as a base64 data URI.",
)
async def proxy_image(
    request: ProxyImageRequest,
    _user: AuthenticatedUser = Depends(require_authenticated()),
):
    """Proxy-fetch an image URL to avoid browser CORS restrictions."""
    url = request.url.strip()
    max_size = 2 * 1024 * 1024

    try:
        content_type, body = await fetch_guarded(url, max_bytes=max_size)
    except SafeHTTPError as exc:
        # The reason names a category, never a resolved address, so a refusal cannot be walked
        # to map the network. Reachable by any authenticated user, tenants included.
        raise HTTPException(status_code=400, detail=str(exc))

    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail=f"URL does not point to an image (content-type: {content_type})",
        )

    b64 = base64.b64encode(body).decode("utf-8")
    return ProxyImageResponse(data=f"data:{content_type};base64,{b64}")
