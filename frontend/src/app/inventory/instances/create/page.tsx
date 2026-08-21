"use client";

import { InstanceCreateForm } from "@/components/instance-create-form";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { RequireRole } from "@/components/permission-guard";
import { Role } from "@/types/rbac";
import { AccessDeniedRedirect } from "@/components/access-denied-redirect";
import { ConsolePageSkeleton } from "@/components/skeletons/console-page-skeleton";


export default function CreateInstancePage() {
  return (
    <RequireRole role={Role.PARTNER} fallback={<AccessDeniedRedirect to="/inventory/instances" />} pending={<ConsolePageSkeleton />}>
      <div className="p-8">
        <ReadOnlyBanner />
        <InstanceCreateForm />
      </div>
    </RequireRole>
  );
}
