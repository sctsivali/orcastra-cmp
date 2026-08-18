/**
 * usePermissions - React hook for three-tier RBAC permission checks
 *
 * Reads the current user's groups from the NextAuth session,
 * resolves them to internal roles (Admin/Partner/Tenant),
 * and exposes helpers for UI gating.
 *
 * NOTE: This is for UI/UX only. Backend always enforces RBAC independently.
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
  /** Session resolved and the user is signed in - a denial is only meaningful when true */
  isAuthenticated: boolean;
  /** Resolved roles */
  roles: Role[];
  /** Highest-privilege role */
  highestRole: Role;
  /** All permissions */
  permissions: Set<Permission>;
  /** Check a single permission */
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
  // unmount and remount its children on each silent token refresh. We only lack an
  // answer when there is no session to read roles from.
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
