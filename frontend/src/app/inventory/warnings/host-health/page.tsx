"use client";

import { HostHealthView } from "@/components/host-health/host-health-view";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";

// The dashboard's own dependencies, apart from the clusters' warnings. Administrators only: the
// page names internal addresses and unseals Vault. auth.ts redirects any other role before this
// renders, and every API it reads refuses them as well.
export default function HostHealthPage() {
  const { allowed, isResolved } = useRequireRole(Role.ADMIN);
  if (!isResolved) return <LoadingLottiePage />;
  if (!allowed) return null;
  return <HostHealthView />;
}
