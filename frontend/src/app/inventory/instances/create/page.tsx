"use client";

import { InstanceCreateForm } from "@/components/instance-create-form";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { RequireRole } from "@/components/permission-guard";
import { Role } from "@/types/rbac";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

function AccessDeniedRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/inventory/instances");
  }, [router]);
  return null;
}

export default function CreateInstancePage() {
  return (
    <RequireRole role={Role.PARTNER} fallback={<AccessDeniedRedirect />}>
      <div className="p-8">
        <ReadOnlyBanner />
        <InstanceCreateForm />
      </div>
    </RequireRole>
  );
}
