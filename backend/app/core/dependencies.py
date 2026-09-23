"""
Dependencies for read-only mode checks, authentication, and RBAC.
"""

from fastapi import HTTPException, status
from app.core.config import get_settings

# re-export auth dependencies for convenient imports
from app.core.auth import (
    get_current_user,
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


# important: this module exists to re-export the auth and RBAC surface so routers have one
# import site. Without __all__ those imports look unused and a linter strips them, which breaks
# every router at import time. Declaring them here makes the intent explicit and keeps ruff's
# F401 honest rather than silenced.
__all__ = [
    "get_current_user",
    "get_ws_user",
    "AuthenticatedUser",
    "Role",
    "Permission",
    "require_roles",
    "require_permissions",
    "require_authenticated",
    "require_admin",
    "require_partner",
    "require_tenant",
    "check_read_only_mode",
]


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
