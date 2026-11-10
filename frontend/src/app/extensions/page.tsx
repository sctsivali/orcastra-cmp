"use client";

import { ExtensionsOverview } from "@/components/extensions/extensions-overview";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";

/**
 * Add-ons, as a route instead of a settings tab.
 *
 * Settings tabs keep no URL state, so a refresh drops back to General. The credential an install
 * issues is shown once, and a refresh inside a tab would destroy the only copy of that secret. A
 * route also makes the role gate visible to scripts/checks/check-role-gates.mjs, which reads
 * `useRequireRole` out of page files.
 */
export default function ExtensionsPage() {
  const { allowed, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!allowed) return null;

  return (
    <div className="container mx-auto space-y-6 p-6">
      <ExtensionsOverview />
    </div>
  );
}
