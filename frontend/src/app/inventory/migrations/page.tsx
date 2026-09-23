"use client";

import { Suspense } from "react";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { MigrationCenter } from "./components/migration-center";

export default function MigrationsPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <MigrationCenter />
    </Suspense>
  );
}
