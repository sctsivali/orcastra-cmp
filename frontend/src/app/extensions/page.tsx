"use client";

import { ExtensionsOverview } from "@/components/extensions/extensions-overview";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";

/**
 * Add-ons, as a route rather than a settings tab.
 *
 * Settings is one page with uncontrolled tabs and no URL state, so a refresh drops you back to
 * General. That is survivable for a preferences panel and not survivable here: the credential an
 * install issues is shown once, and an accidental refresh inside a tab would destroy the only
 * copy of a secret. A route also makes the role gate visible to the harness that reads
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
