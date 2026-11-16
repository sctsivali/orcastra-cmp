/**
 * Whether Rebuild is offered to this person for one instance. Every surface that offers it (the
 * Instances row menu, the console toolbar, the full-page terminal) asks this, so they cannot drift
 * apart again (scripts/checks/check-rebuild-gate-parity.mjs).
 *
 * Partner and Admin rebuild what they can manage. A Tenant needs View, Operate and Rebuild on the
 * instance, the same three the server checks (backend core/instance_access.py), and the deployment
 * must not have turned tenant rebuild off.
 */
export interface RebuildAccessInput {
  isPartner: boolean;
  hasPermission: (permission: "view" | "operate" | "rebuild") => boolean;
  /**
   * From /system/settings. important: only an explicit true offers it to a Tenant. A backend that
   * does not state it predates tenant rebuild and refuses it.
   */
  tenantRebuildEnabled: boolean | undefined;
}

export function rebuildOffered({ isPartner, hasPermission, tenantRebuildEnabled }: RebuildAccessInput): boolean {
  if (isPartner) return true;
  return (
    tenantRebuildEnabled === true &&
    hasPermission("view") &&
    hasPermission("operate") &&
    hasPermission("rebuild")
  );
}
