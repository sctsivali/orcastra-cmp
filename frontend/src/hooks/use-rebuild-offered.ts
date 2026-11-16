"use client";

import { usePermissions } from "@/hooks/use-permissions";
import { useTenantRebuildEnabled } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { rebuildOffered } from "@/lib/rebuild-access";
import { Role } from "@/types/rbac";

/** lib/rebuild-access.ts for one instance, for a surface that shows one instance. */
export function useRebuildOffered(host: string, project: string, instanceName: string): boolean {
  const { hasRole } = usePermissions();
  const { hasPermission } = useTenantAccess();
  const tenantRebuildEnabled = useTenantRebuildEnabled();
  return rebuildOffered({
    isPartner: hasRole(Role.PARTNER),
    hasPermission: (permission) => hasPermission(host, project, instanceName, permission),
    tenantRebuildEnabled,
  });
}
