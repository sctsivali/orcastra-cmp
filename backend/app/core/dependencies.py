"""FastAPI dependencies for read-only mode, authentication and RBAC."""

from fastapi import HTTPException, status
from app.core.config import get_settings

# Auth and RBAC re-exported so routers have one import site.
from app.core.auth import (
    get_current_user,
    get_ws_user,
    AuthenticatedUser,
)

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


# important: without __all__ the re-exports look unused and a linter strips them, which breaks
# every router at import time.
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
    """Refuse the request with 403 while READ_ONLY_MODE is on."""
    settings = get_settings()
    if settings.read_only_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation not allowed: Application is in read-only mode"
        )
