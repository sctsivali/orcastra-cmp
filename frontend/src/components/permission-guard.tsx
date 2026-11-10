"use client";

/**
 * Renders children only when the session holds a permission or a minimum role.
 *
 *   <PermissionGuard permission={Permission.INSTANCE_DELETE}>
 *     <DeleteButton />
 *   </PermissionGuard>
 *
 * A UI helper only; the backend enforces authorization independently.
 */

import { ReactNode } from "react";
import { usePermissions } from "@/hooks/use-permissions";
import { Role, Permission } from "@/types/rbac";

interface PermissionGuardProps {
  children: ReactNode;
  /** Children are hidden unless the user holds this permission. */
  permission?: Permission;
  /** Children are hidden unless the user meets this role level. */
  minRole?: Role;
  /** Content shown when the check fails. Defaults to nothing. */
  fallback?: ReactNode;
  /** Disable children instead of hiding them (opacity plus pointer-events: none). */
  disableInsteadOfHide?: boolean;
  /**
   * Content shown while the session is still resolving. Defaults to nothing, which suits an
   * inline guard around a button; a guard wrapping a whole page would otherwise paint a blank
   * screen after the route has committed.
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

  // Hide guarded content while the session resolves, so it does not flash.
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

/** Shorthand for a guard that checks only a minimum role. */
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
