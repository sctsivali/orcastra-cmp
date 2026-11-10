/**
 * Three-tier RBAC checks for UI gating: the session's groups resolved to Admin, Partner or
 * Tenant roles and their permissions.
 *
 * UI only. The backend enforces RBAC on its own.
 */

"use client";

import { useMemo } from "react";
import { useSession } from "next-auth/react";
import {
  Role,
  Permission,
  resolveRoles,
  getHighestRole,
  getUserPermissions,
  hasPermission,
  hasMinimumRole,
} from "@/types/rbac";

export interface UsePermissionsResult {
  /** No permission answer available yet (first load only, not a background refresh) */
  isLoading: boolean;
  /** Session resolved and the user is signed in; a denial counts only when true */
  isAuthenticated: boolean;
  roles: Role[];
  /** Highest-privilege role */
  highestRole: Role;
  permissions: Set<Permission>;
  can: (permission: Permission) => boolean;
  /** Check minimum role level */
  hasRole: (minRole: Role) => boolean;
  /** Convenience: is Admin */
  isAdmin: boolean;
  /** Convenience: Partner or Admin */
  isPartner: boolean;
  /** Convenience: any authenticated user */
  isTenant: boolean;
  /** Raw Authentik groups */
  groups: string[];
}

export function usePermissions(): UsePermissionsResult {
  const { data: session, status } = useSession();
  // important: useSession().update() flips status back to "loading" while keeping the
  // existing session data. Treating that as "loading" makes every PermissionGuard
  // unmount and remount its children on each silent token refresh, so only a missing
  // session counts as loading.
  const isLoading = status === "loading" && !session;
  const isAuthenticated = status === "authenticated";

  const groups: string[] = useMemo(() => {
    return (session?.user as any)?.groups ?? [];
  }, [session]);

  const roles = useMemo(() => resolveRoles(groups), [groups]);
  const highestRole = useMemo(() => getHighestRole(roles), [roles]);
  const permissions = useMemo(() => getUserPermissions(roles), [roles]);

  const can = useMemo(
    () => (permission: Permission) => hasPermission(roles, permission),
    [roles]
  );

  const hasRoleFn = useMemo(
    () => (minRole: Role) => hasMinimumRole(roles, minRole),
    [roles]
  );

  return {
    isLoading,
    isAuthenticated,
    roles,
    highestRole,
    permissions,
    can,
    hasRole: hasRoleFn,
    isAdmin: hasMinimumRole(roles, Role.ADMIN),
    isPartner: hasMinimumRole(roles, Role.PARTNER),
    isTenant: hasMinimumRole(roles, Role.TENANT),
    groups,
  };
}
