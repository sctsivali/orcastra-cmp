"""
API Router for User Preferences/Personalization.
Handles CRUD operations for per-user dashboard customization.

Architecture:
- PostgreSQL is the SINGLE SOURCE OF TRUTH for user preferences
- Frontend localStorage is only used as offline cache/fallback
- All settings are keyed by user_id (OIDC subject claim from Authentik)
- Cross-browser sync is achieved through server-side storage
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


# important: these values are the product defaults, and they must match
# frontend/src/types/personalization.ts, the CSS seeds in frontend/src/app/globals.css and
# get_default_settings() below. This is the copy a user with no stored row actually receives,
# so a drift here is not a styling nit: the API hands out one colour while the stylesheet
# paints another until the row is written. scripts/checks/check-personalization-defaults.mjs
# reads this file and fails the build when the copies disagree.
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


# important: identity comes from the validated JWT principal, never from a request
# header. These endpoints used to key on X-User-ID, which any authenticated caller
# could set to somebody else's subject and so read, overwrite or delete another
# user's preferences. The header is now ignored entirely.
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
    """
    Get personalization settings for the current user.
    
    This endpoint is the source of truth for user preferences.
    Frontend should always fetch from here on login to ensure
    cross-browser synchronization.
    """
    
    # Set cache-control headers to prevent browser/proxy caching
    # This ensures fresh data is always fetched from the database
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    x_user_id, x_user_email = current_identity(user)
    
    logger.info(f"Fetching preferences for user {x_user_id}")
    
    try:
        # Query user preferences
        result = await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == x_user_id)
        )
        user_prefs = result.scalar_one_or_none()
        
        if user_prefs:
            # Merge with defaults to ensure all fields exist
            settings = get_default_settings()
            saved_settings = user_prefs.settings or {}
            
            # Deep merge saved settings into defaults
            for key in ["logo", "background", "branding", "colors"]:
                if key in saved_settings:
                    settings[key].update(saved_settings[key])
            
            # Preserve scalar top-level keys (theme, etc.)
            for key in saved_settings:
                if key not in ["logo", "background", "branding", "colors", "lastModified"]:
                    settings[key] = saved_settings[key]
            
            if "lastModified" in saved_settings:
                settings["lastModified"] = saved_settings["lastModified"]
            else:
                settings["lastModified"] = user_prefs.updated_at.isoformat()
            
            logger.info(f"Returning saved preferences for user {x_user_id}, lastModified: {settings['lastModified']}")
            return settings
        else:
            # No saved preferences, return defaults
            logger.info(f"No preferences found for user {x_user_id}, returning defaults")
            return get_default_settings()
            
    except Exception as e:
        logger.error(f"Error fetching user preferences for {x_user_id}: {e}")
        # Return defaults on error to avoid breaking the UI
        return get_default_settings()


@router.put(
    "",
    response_model=Dict[str, Any],
    summary="Update user preferences",
    description="Save personalization settings for the authenticated user. This is the authoritative write operation."
)
async def update_user_preferences(
    settings: Dict[str, Any],
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_authenticated()),
    _: None = Depends(check_read_only_mode),
) -> Dict[str, Any]:
    """
    Update personalization settings for the current user.
    
    This endpoint saves settings to the database (source of truth).
    Frontend should update both localStorage cache and call this API.
    The timestamp is set server-side to ensure consistency across browsers.
    """
    
    # Set cache-control headers
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    
    x_user_id, x_user_email = current_identity(user)
    
    logger.info(f"Updating preferences for user {x_user_id}")
    
    try:
        # Check if user preferences exist
        result = await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == x_user_id)
        )
        user_prefs = result.scalar_one_or_none()
        
        # Set lastModified timestamp server-side (authoritative)
        now = datetime.now(timezone.utc)
        settings["lastModified"] = now.isoformat()
        
        if user_prefs:
            # Update existing preferences
            user_prefs.settings = settings
            user_prefs.user_email = x_user_email
            user_prefs.updated_at = now
            logger.info(f"Updated existing preferences for user {x_user_id}, lastModified: {settings['lastModified']}")
        else:
            # Create new preferences
            user_prefs = UserPreferences(
                user_id=x_user_id,
                user_email=x_user_email,
                settings=settings,
            )
            db.add(user_prefs)
            logger.info(f"Created new preferences for user {x_user_id}, lastModified: {settings['lastModified']}")
        
        await db.commit()
        await db.refresh(user_prefs)
        
        # Return the complete merged settings with defaults
        merged_settings = get_default_settings()
        saved_settings = user_prefs.settings or {}
        for key in ["logo", "background", "branding", "colors"]:
            if key in saved_settings:
                merged_settings[key].update(saved_settings[key])
        # Preserve scalar top-level keys (theme, etc.)
        for key in saved_settings:
            if key not in ["logo", "background", "branding", "colors", "lastModified"]:
                merged_settings[key] = saved_settings[key]
        merged_settings["lastModified"] = saved_settings.get("lastModified")
        
        return merged_settings
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Error saving user preferences for {x_user_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save preferences: {str(e)}"
        )


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
    """
    Reset personalization settings to defaults.
    
    Deletes the user's preferences from the database,
    effectively resetting all customizations to system defaults.
    """
    
    # Set cache-control headers
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
        raise HTTPException(status_code=500, detail="Failed to reset preferences")


# ============================================
# Image Proxy - fetch cross-origin images server-side
# ============================================

# note: the private-range list and the URL check that used to live here were a second, older
# copy of core/url_safety.py. It had drifted: no 0.0.0.0/8, no fe80::/10, no scheme check, and
# it missed every IPv4-mapped IPv6 form. Two copies of an SSRF filter guarantees one goes
# stale, so this surface now uses the same guarded client as every other outbound fetch.


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
