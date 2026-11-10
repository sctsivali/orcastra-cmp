/**
 * RBAC roles, permissions and the Authentik group mapping, mirroring the backend's three
 * tiers: Admin, Partner, Tenant. Hooks and components use them to decide what to render.
 *
 * important: the backend enforces authorization; nothing here is a security boundary.
 */

// ---------------------------------------------------------------------------
// Roles - strict three-tier
// ---------------------------------------------------------------------------

export enum Role {
  ADMIN = "admin",
  PARTNER = "partner",
  TENANT = "tenant",
}

/** Hierarchy level; higher is more privileged. */
export const ROLE_HIERARCHY: Record<Role, number> = {
  [Role.TENANT]: 0,
  [Role.PARTNER]: 1,
  [Role.ADMIN]: 2,
};

export const ROLE_LABELS: Record<Role, string> = {
  [Role.ADMIN]: "Administrator",
  [Role.PARTNER]: "Partner",
  [Role.TENANT]: "Tenant",
};

// ---------------------------------------------------------------------------
// Permissions - mirrors backend Permission enum
// ---------------------------------------------------------------------------

export enum Permission {
  CLUSTER_READ = "cluster:read",
  CLUSTER_WRITE = "cluster:write",
  CLUSTER_DELETE = "cluster:delete",
  INSTANCE_READ = "instance:read",
  INSTANCE_WRITE = "instance:write",
  INSTANCE_DELETE = "instance:delete",
  INSTANCE_CONSOLE = "instance:console",
  INSTANCE_TERMINAL = "instance:terminal",
  NETWORK_READ = "network:read",
  NETWORK_WRITE = "network:write",
  STORAGE_READ = "storage:read",
  STORAGE_WRITE = "storage:write",
  IMAGE_READ = "image:read",
  IMAGE_WRITE = "image:write",
  PROFILE_READ = "profile:read",
  PROFILE_WRITE = "profile:write",
  PROJECT_READ = "project:read",
  PROJECT_WRITE = "project:write",
  CERT_READ = "cert:read",
  CERT_WRITE = "cert:write",
  MONITORING_READ = "monitoring:read",
  AUDIT_READ = "audit:read",
  AUDIT_ADMIN = "audit:admin",
  SYSTEM_READ = "system:read",
  SYSTEM_WRITE = "system:write",
  PERMISSION_READ = "permission:read",
  PERMISSION_WRITE = "permission:write",
  TAG_READ = "tag:read",
  TAG_WRITE = "tag:write",
  ARCHIVE_READ = "archive:read",
  ARCHIVE_WRITE = "archive:write",
  USER_PREF_READ = "user_pref:read",
  USER_PREF_WRITE = "user_pref:write",
  FILE_TRANSFER = "file_transfer:write",
  INTEGRATION_READ = "integration:read",
  INTEGRATION_WRITE = "integration:write",
  ORG_READ = "org:read",
  ORG_WRITE = "org:write",
  ORG_DELETE = "org:delete",
  ORG_MEMBER_READ = "org:member:read",
  ORG_MEMBER_WRITE = "org:member:write",
}

// ---------------------------------------------------------------------------
// Permission matrix - three-tier
// ---------------------------------------------------------------------------

const ALL_PERMISSIONS = new Set(Object.values(Permission));

export const ROLE_PERMISSIONS: Record<Role, Set<Permission>> = {
  [Role.ADMIN]: ALL_PERMISSIONS,

  [Role.PARTNER]: new Set([
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
  ]),

  [Role.TENANT]: new Set([
    Permission.INSTANCE_READ,
    Permission.INSTANCE_CONSOLE, Permission.INSTANCE_TERMINAL,
    Permission.FILE_TRANSFER,
    Permission.NETWORK_READ,
    Permission.STORAGE_READ,
    Permission.IMAGE_READ,
    Permission.PROFILE_READ,
    Permission.PROJECT_READ,
    Permission.MONITORING_READ,
    Permission.TAG_READ,
    Permission.USER_PREF_READ, Permission.USER_PREF_WRITE,
    Permission.ORG_READ, Permission.ORG_MEMBER_READ,
  ]),
};

// ---------------------------------------------------------------------------
// Authentik group → Role mapping
// ---------------------------------------------------------------------------

export const AUTHENTIK_GROUP_MAP: Record<string, Role> = {
  // Canonical names; create these groups in Authentik.
  role_admin: Role.ADMIN,
  role_partner: Role.PARTNER,
  role_tenant: Role.TENANT,
  // Short aliases.
  admin: Role.ADMIN,
  partner: Role.PARTNER,
  tenant: Role.TENANT,
  admins: Role.ADMIN,
  // Authentik's own admin group and other common group names.
  "authentik admins": Role.ADMIN,
  "orcastra-admins": Role.ADMIN,
  "orcastra-partners": Role.PARTNER,
  "orcastra-tenants": Role.TENANT,
};

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

/** The roles these Authentik groups map to, with no default: empty means no role at all. */
export function resolveMappedRoles(groups: string[]): Role[] {
  const roles = new Set<Role>();
  for (const g of groups) {
    const key = g.toLowerCase().trim();
    if (key in AUTHENTIK_GROUP_MAP) {
      roles.add(AUTHENTIK_GROUP_MAP[key]);
    } else if (key.startsWith("role_")) {
      const stripped = key.slice(5);
      if (stripped in AUTHENTIK_GROUP_MAP) {
        roles.add(AUTHENTIK_GROUP_MAP[stripped]);
      }
    }
  }
  return Array.from(roles);
}

/** Whether any group maps to an Orcastra role. The backend refuses a session without one. */
export function hasMappedRole(groups: string[]): boolean {
  return resolveMappedRoles(groups).length > 0;
}

/**
 * Resolve Authentik groups to roles, falling back to Tenant.
 *
 * The fallback only gives permission checks a role to compare against. Whether the caller
 * may use the dashboard at all is hasMappedRole, checked first by MainLayout's access gate.
 */
export function resolveRoles(groups: string[]): Role[] {
  const roles = resolveMappedRoles(groups);
  return roles.length > 0 ? roles : [Role.TENANT];
}

/** The most privileged of these roles; Tenant when there are none. */
export function getHighestRole(roles: Role[]): Role {
  if (roles.length === 0) return Role.TENANT;
  return roles.reduce((a, b) =>
    (ROLE_HIERARCHY[a] ?? 0) >= (ROLE_HIERARCHY[b] ?? 0) ? a : b
  );
}

/** Merge permissions from all roles. */
export function getUserPermissions(roles: Role[]): Set<Permission> {
  const perms = new Set<Permission>();
  for (const role of roles) {
    for (const p of ROLE_PERMISSIONS[role] ?? []) {
      perms.add(p);
    }
  }
  return perms;
}

/** Check a single permission against roles. */
export function hasPermission(roles: Role[], permission: Permission): boolean {
  return getUserPermissions(roles).has(permission);
}

/** Check if user meets minimum role level. */
export function hasMinimumRole(roles: Role[], minRole: Role): boolean {
  const highest = getHighestRole(roles);
  return (ROLE_HIERARCHY[highest] ?? 0) >= (ROLE_HIERARCHY[minRole] ?? 0);
}
