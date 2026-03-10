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
  /** Session still loading */
  isLoading: boolean;
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
  const isLoading = status === "loading";

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
