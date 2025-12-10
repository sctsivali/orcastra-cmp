"""
System settings and configuration endpoints.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from app.core.config import get_settings

router = APIRouter(prefix="/system", tags=["system"])


class SystemSettings(BaseModel):
    """System settings response model."""
    read_only_mode: bool
    api_version: str
    debug: bool


@router.get("/settings", response_model=SystemSettings)
async def get_system_settings():
    """
    Get current system settings.
    
    Returns information about the system configuration including
    whether read-only mode is enabled.
    """
    settings = get_settings()
    return SystemSettings(
        read_only_mode=settings.read_only_mode,
        api_version=settings.api_version,
        debug=settings.debug
    )
