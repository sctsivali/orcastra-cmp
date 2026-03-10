"""
Dependencies for read-only mode checks, authentication, and RBAC.
"""

from fastapi import HTTPException, status
from app.core.config import get_settings

# re-export auth dependencies for convenient imports
from app.core.auth import (
    get_current_user,
    get_optional_user,
    get_ws_user,
    AuthenticatedUser,
)

# re-export RBAC dependencies for convenient imports
from app.core.rbac import (
    Role,
    Permission,
    require_roles,
    require_permissions,
    require_authenticated,
    require_admin,
    require_partner,
    require_tenant,
)


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
