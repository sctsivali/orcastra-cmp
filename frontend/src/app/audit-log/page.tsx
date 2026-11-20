"use client";

import { Suspense } from "react";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { AuditLogView } from "@/editions/mini/audit-log-view";

// important: Administrators only, here, in auth.ts and on the backend (require_admin).
export default function AuditLogPage() {
  const { allowed, isResolved } = useRequireRole(Role.ADMIN);
  if (!isResolved) return <LoadingLottiePage />;
  if (!allowed) return null;
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <AuditLogView />
    </Suspense>
  );
}
