"""
Dependencies for read-only mode checks.
"""

from fastapi import HTTPException, status
from app.core.config import get_settings


def check_read_only_mode():
    """
    Dependency to check if the application is in read-only mode.
    Raises HTTPException if read-only mode is enabled.
    """
    settings = get_settings()
    if settings.read_only_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation not allowed: Application is in read-only mode"
        )
