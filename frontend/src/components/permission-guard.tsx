"use client";

/**
 * PermissionGuard - conditionally renders children based on RBAC.
 *
 * Usage:
 *   <PermissionGuard permission={Permission.INSTANCE_DELETE}>
 *     <DeleteButton />
 *   </PermissionGuard>
 *
 *   <PermissionGuard minRole={Role.ADMIN}>
 *     <AdminPanel />
 *   </PermissionGuard>
 *
 *   <PermissionGuard permission={Permission.INSTANCE_WRITE} fallback={<span>Read-only</span>}>
 *     <EditButton />
 *   </PermissionGuard>
 *
 * NOTE: This is a UI helper only. Backend enforces authorization independently.
 */

import { ReactNode } from "react";
import { usePermissions } from "@/hooks/use-permissions";
import { Role, Permission } from "@/types/rbac";

interface PermissionGuardProps {
  children: ReactNode;
  /** Required permission - child is hidden unless user holds this permission */
  permission?: Permission;
  /** Required minimum role - child is hidden unless user meets this hierarchy level */
  minRole?: Role;
  /** Content shown when permission check fails (default: nothing) */
  fallback?: ReactNode;
  /** If true, disable children instead of hiding (adds opacity + pointer-events:none) */
  disableInsteadOfHide?: boolean;
  /**
   * Content shown while the session is still resolving. Defaults to nothing, which is right
   * for an inline guard around a button but wrong for a guard wrapping a whole page: there it
   * paints a blank screen after the route has already committed.
   */
  pending?: ReactNode;
}

export function PermissionGuard({
  children,
  permission,
  minRole,
  fallback = null,
  disableInsteadOfHide = false,
  pending = null,
}: PermissionGuardProps) {
  const { can, hasRole, isLoading } = usePermissions();

  // while loading session, hide guarded content to avoid flash
  if (isLoading) return <>{pending}</>;

  let allowed = true;

  if (permission) {
    allowed = can(permission);
  }

  if (allowed && minRole) {
    allowed = hasRole(minRole);
  }

  if (allowed) {
    return <>{children}</>;
  }

  if (disableInsteadOfHide) {
    return (
      <div
        style={{ opacity: 0.4, pointerEvents: "none", cursor: "not-allowed" }}
        aria-disabled="true"
        title="You don't have permission for this action"
      >
        {children}
      </div>
    );
  }

  return <>{fallback}</>;
}

/**
 * RequireRole - shorthand that only checks minimum role.
 */
export function RequireRole({
  role,
  children,
  fallback,
  pending,
}: {
  role: Role;
  children: ReactNode;
  fallback?: ReactNode;
  pending?: ReactNode;
}) {
  return (
    <PermissionGuard minRole={role} fallback={fallback} pending={pending}>
      {children}
    </PermissionGuard>
  );
}
