"use client";

import { Suspense } from "react";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { OperatorsView } from "@/editions/mini/operators/operators-view";

// important: the role gate. A Tenant deep-linking here is sent home before any request; the
// backend (require_partner on /operators) is the real gate. auth.ts refuses the route too.
export default function OperatorsPage() {
  const { allowed, isResolved } = useRequireRole(Role.PARTNER);
  if (!isResolved) return <LoadingLottiePage />;
  if (!allowed) return null;
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <OperatorsView />
    </Suspense>
  );
}
