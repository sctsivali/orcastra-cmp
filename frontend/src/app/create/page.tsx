"use client";

import { ProjectCreateForm } from "@/components/project-create-form";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { RequireRole } from "@/components/permission-guard";
import { Role } from "@/types/rbac";
import { AccessDeniedRedirect } from "@/components/access-denied-redirect";
import { ConsolePageSkeleton } from "@/components/skeletons/console-page-skeleton";


export default function CreatePage() {
  return (
    <RequireRole role={Role.PARTNER} fallback={<AccessDeniedRedirect to="/" />} pending={<ConsolePageSkeleton />}>
      <ReadOnlyBanner />
      <ProjectCreateForm />
    </RequireRole>
  );
}
