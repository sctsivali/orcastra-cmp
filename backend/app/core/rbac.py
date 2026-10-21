"""
RBAC (Role-Based Access Control) Module (Three-Tier Model)

Implements the ORCA Operations Center Dashboard three-tier RBAC:
  1. Admin: full system-wide access
  2. Partner: full control over own clusters / organizations
  3. Tenant: project-level access within assigned organizations

Authentik group naming convention:
  - role_admin   → Admin
  - role_partner → Partner
  - role_tenant  → Tenant
"""

import logging
from enum import Enum
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.request_context import get_request_context
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()


# ---------------------------------------------------------------------------
# Role definitions - strict three-tier
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """Internal roles mapped from Authentik groups."""
    ADMIN = "admin"      # System-wide access
    PARTNER = "partner"  # Cluster owner: own clusters, projects, tenants
    TENANT = "tenant"    # End user: assigned projects only


# Role hierarchy: higher index = more privileged
ROLE_HIERARCHY: dict[Role, int] = {
    Role.TENANT: 0,
    Role.PARTNER: 1,
    Role.ADMIN: 2,
}

# Map Authentik group names → internal Role
# Keys MUST be lowercase - resolve_user_role() lowercases before lookup.
AUTHENTIK_GROUP_MAP: dict[str, Role] = {
    # canonical names (recommended - create these in Authentik)
    "role_admin": Role.ADMIN,
    "role_partner": Role.PARTNER,
    "role_tenant": Role.TENANT,
    # short aliases
    "admin": Role.ADMIN,
    "partner": Role.PARTNER,
    "tenant": Role.TENANT,
    "admins": Role.ADMIN,
    # common Authentik default groups
    "authentik admins": Role.ADMIN,
    "orcastra-admins": Role.ADMIN,
    "orcastra-partners": Role.PARTNER,
    "orcastra-tenants": Role.TENANT,
}


# ---------------------------------------------------------------------------
# Permission definitions
# ---------------------------------------------------------------------------

class Permission(str, Enum):
    """Fine-grained permissions for resource-level checks."""
    CLUSTER_READ = "cluster:read"
    CLUSTER_WRITE = "cluster:write"
    CLUSTER_DELETE = "cluster:delete"
    INSTANCE_READ = "instance:read"
    INSTANCE_WRITE = "instance:write"
    INSTANCE_DELETE = "instance:delete"
    INSTANCE_CONSOLE = "instance:console"
    INSTANCE_TERMINAL = "instance:terminal"
    NETWORK_READ = "network:read"
    NETWORK_WRITE = "network:write"
    STORAGE_READ = "storage:read"
    STORAGE_WRITE = "storage:write"
    IMAGE_READ = "image:read"
    IMAGE_WRITE = "image:write"
    PROFILE_READ = "profile:read"
    PROFILE_WRITE = "profile:write"
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    CERT_READ = "cert:read"
    CERT_WRITE = "cert:write"
    MONITORING_READ = "monitoring:read"
    AUDIT_READ = "audit:read"
    AUDIT_ADMIN = "audit:admin"
    SYSTEM_READ = "system:read"
    SYSTEM_WRITE = "system:write"
    PERMISSION_READ = "permission:read"
    PERMISSION_WRITE = "permission:write"
    TAG_READ = "tag:read"
    TAG_WRITE = "tag:write"
    ARCHIVE_READ = "archive:read"
    ARCHIVE_WRITE = "archive:write"
    USER_PREF_READ = "user_pref:read"
    USER_PREF_WRITE = "user_pref:write"
    FILE_TRANSFER = "file_transfer:write"
    INTEGRATION_READ = "integration:read"
    INTEGRATION_WRITE = "integration:write"
    ORG_READ = "org:read"
    ORG_WRITE = "org:write"
    ORG_DELETE = "org:delete"
    ORG_MEMBER_READ = "org:member:read"
    ORG_MEMBER_WRITE = "org:member:write"


# Permission matrix - three-tier
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # full access

    Role.PARTNER: {
        Permission.CLUSTER_READ, Permission.CLUSTER_WRITE,
        Permission.INSTANCE_READ, Permission.INSTANCE_WRITE, Permission.INSTANCE_DELETE,
        Permission.INSTANCE_CONSOLE, Permission.INSTANCE_TERMINAL,
        Permission.NETWORK_READ, Permission.NETWORK_WRITE,
        Permission.STORAGE_READ, Permission.STORAGE_WRITE,
        Permission.IMAGE_READ, Permission.IMAGE_WRITE,
        Permission.PROFILE_READ, Permission.PROFILE_WRITE,
        Permission.PROJECT_READ, Permission.PROJECT_WRITE,
        Permission.CERT_READ, Permission.CERT_WRITE,
        Permission.MONITORING_READ,
        Permission.TAG_READ, Permission.TAG_WRITE,
        Permission.USER_PREF_READ, Permission.USER_PREF_WRITE,
        Permission.FILE_TRANSFER,
        Permission.INTEGRATION_READ,
        Permission.ORG_READ, Permission.ORG_WRITE,
        Permission.ORG_MEMBER_READ, Permission.ORG_MEMBER_WRITE,
    },

    Role.TENANT: {
        Permission.INSTANCE_READ,
        Permission.INSTANCE_CONSOLE, Permission.INSTANCE_TERMINAL,
        Permission.NETWORK_READ,
        Permission.STORAGE_READ,
        Permission.IMAGE_READ,
        Permission.PROFILE_READ,
        Permission.PROJECT_READ,
        Permission.MONITORING_READ,
        Permission.TAG_READ,
        Permission.USER_PREF_READ, Permission.USER_PREF_WRITE,
        Permission.ORG_READ, Permission.ORG_MEMBER_READ,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mapped_roles(group_names: list[str]) -> list[Role]:
    """The roles these Authentik group names map to, with no default: empty means no role."""
    roles: list[Role] = []
    for group in group_names:
        group_lower = group.lower().strip()
        if group_lower in AUTHENTIK_GROUP_MAP:
            roles.append(AUTHENTIK_GROUP_MAP[group_lower])
        elif group_lower.startswith("role_"):
            stripped = group_lower[5:]
            if stripped in AUTHENTIK_GROUP_MAP:
                roles.append(AUTHENTIK_GROUP_MAP[stripped])
    return list(set(roles))


def _resolve_roles_uncached(user: AuthenticatedUser) -> list[Role]:
    """Map Authentik groups to roles (no cache).

    note: still answers TENANT for a caller with no role group, because tenant grants, the
    integration rebind and the role reader resolve through here and a key or membership row is not
    a session. Whether a session may act at all is decided by refuse_unmapped below.
    """
    return mapped_roles(user.groups) or [Role.TENANT]


def roles_for_group_names(group_names: list[str]) -> list[Role]:
    """Roles for these Authentik group names, resolved exactly as a request's groups claim is."""
    return _resolve_roles_uncached(AuthenticatedUser(user_id="", groups=list(group_names)))


def resolve_roles(user: AuthenticatedUser) -> list[Role]:
    """Map the caller's Authentik groups to roles.

    important: computed from the token's own groups on every call, never cached. A per-user cache in
    Redis used to outrank the token for a minute: its entries were unsigned, so anyone able to write
    to Redis could give themselves Admin, and a demoted user kept the old role until it expired. The
    mapping is a few dictionary lookups, so there is nothing to save by caching it.
    """
    return _resolve_roles_uncached(user)


def get_highest_role(roles: list[Role]) -> Role:
    """Return the most-privileged role."""
    if not roles:
        return Role.TENANT
    return max(roles, key=lambda r: ROLE_HIERARCHY.get(r, 0))


def is_admin(user: AuthenticatedUser) -> bool:
    """Whether the caller is an Administrator, from its groups as require_admin decides it.

    important: not `Role.ADMIN.value in user.roles`. AuthenticatedUser.roles holds the token's raw
    "role_*" group names ("role_admin"), so that test was never true and an Administrator was treated
    as a partner of its own organizations only.
    """
    return get_highest_role(resolve_roles(user)) == Role.ADMIN


def get_user_permissions(roles: list[Role]) -> set[Permission]:
    """Merge permissions across all user roles."""
    perms: set[Permission] = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(role, set())
    return perms


def has_permission(user: AuthenticatedUser, permission: Permission) -> bool:
    """Check whether user holds a specific permission."""
    roles = resolve_roles(user)
    return permission in get_user_permissions(roles)


NO_ROLE_ASSIGNED = "no_role_assigned"
NO_ROLE_ASSIGNED_MESSAGE = (
    "This account has no role in Orcastra yet. Ask an administrator to assign one."
)


def has_mapped_role(user: AuthenticatedUser) -> bool:
    """Whether any of the caller's groups maps to an Orcastra role."""
    return bool(mapped_roles(user.groups))


def refuse_unmapped(request: Request, user: AuthenticatedUser) -> None:
    """Refuse a signed-in caller whose groups map to no role (issue #17).

    important: such a caller used to resolve to Tenant and receive 200 with empty lists, which reads
    as a broken or emptied deployment. A role is now required before any session route answers.
    Integration keys and MCP authenticate elsewhere and never reach this.
    """
    if has_mapped_role(user):
        return
    request.state.user_role = None
    logger.warning(f"RBAC denied (no role): user={user.user_id} path={request.url.path}")
    activity_logger.log_activity(
        action="rbac.access_denied",
        target_type="endpoint",
        target_id=request.url.path,
        result="denied",
        details={"reason": NO_ROLE_ASSIGNED, "method": request.method},
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": NO_ROLE_ASSIGNED, "message": NO_ROLE_ASSIGNED_MESSAGE},
    )


def _populate_request_state(request: Request, user: AuthenticatedUser) -> None:
    """Write resolved RBAC info into request.state AND RequestContext for logging."""
    roles = resolve_roles(user)
    highest = get_highest_role(roles)
    request.state.user_id = user.user_id
    request.state.user_email = user.email
    request.state.user_groups = user.groups
    request.state.user_role = highest.value
    request.state.user_type = highest.value  # alias, kept for compat
    request.state.rbac_roles = [r.value for r in roles]
    request.state.rbac_permissions = [p.value for p in get_user_permissions(roles)]

    # sync resolved role into the RequestContext ContextVar so that
    # ActivityLogger and AccessLogMiddleware read the correct values
    ctx = get_request_context()
    if ctx is not None:
        ctx.user_id = user.user_id
        ctx.user_role = highest.value
        ctx.user_groups = user.groups


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def require_roles(*allowed_roles: Role):
    """Ensures user holds at least one of allowed_roles (hierarchy-aware)."""
    async def _dependency(
        request: Request,
        user: Optional[AuthenticatedUser] = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if user is None:
            return AuthenticatedUser(
                user_id="dev-user", email="dev@localhost",
                groups=["admin"], roles=["role_admin"],
            )
        _populate_request_state(request, user)
        refuse_unmapped(request, user)
        user_roles = resolve_roles(user)
        user_max = max(ROLE_HIERARCHY.get(r, 0) for r in user_roles)
        required_min = min(ROLE_HIERARCHY.get(r, 0) for r in allowed_roles)

        if user_max >= required_min:
            return user

        logger.warning(
            f"RBAC denied: user={user.user_id} roles={[r.value for r in user_roles]} "
            f"required={[r.value for r in allowed_roles]} path={request.url.path}"
        )
        activity_logger.log_activity(
            action="rbac.access_denied",
            target_type="endpoint",
            target_id=request.url.path,
            result="denied",
            details={
                "user_roles": [r.value for r in user_roles],
                "required_roles": [r.value for r in allowed_roles],
                "method": request.method,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    return _dependency


def require_permissions(*required_permissions: Permission):
    """Ensures user has ALL specified permissions."""
    async def _dependency(
        request: Request,
        user: Optional[AuthenticatedUser] = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if user is None:
            return AuthenticatedUser(
                user_id="dev-user", email="dev@localhost",
                groups=["admin"], roles=["role_admin"],
            )
        _populate_request_state(request, user)
        refuse_unmapped(request, user)
        user_roles = resolve_roles(user)
        user_perms = get_user_permissions(user_roles)
        missing = set(required_permissions) - user_perms

        if not missing:
            return user

        logger.warning(
            f"RBAC denied (permission): user={user.user_id} "
            f"missing={[p.value for p in missing]} path={request.url.path}"
        )
        activity_logger.log_activity(
            action="rbac.access_denied",
            target_type="endpoint",
            target_id=request.url.path,
            result="denied",
            details={
                "user_roles": [r.value for r in user_roles],
                "missing_permissions": [p.value for p in missing],
                "method": request.method,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    return _dependency


def require_authenticated():
    """Any authenticated user. Populates request.state for logging."""
    async def _dependency(
        request: Request,
        user: Optional[AuthenticatedUser] = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if user is None:
            return AuthenticatedUser(
                user_id="dev-user", email="dev@localhost",
                groups=["admin"], roles=["role_admin"],
            )
        _populate_request_state(request, user)
        refuse_unmapped(request, user)
        return user
    return _dependency


# ---------------------------------------------------------------------------
# Convenience aliases - three-tier
# ---------------------------------------------------------------------------

def require_admin():
    """Admin only - system-wide access."""
    return require_roles(Role.ADMIN)


def require_partner():
    """Partner or above (Partner + Admin)."""
    return require_roles(Role.PARTNER)


def require_tenant():
    """Tenant or above (all three tiers)."""
    return require_roles(Role.TENANT)
