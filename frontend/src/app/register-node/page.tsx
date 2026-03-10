"use client";

import { NodeRegisterForm } from "@/components/node-register-form";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { RequireRole } from "@/components/permission-guard";
import { Role } from "@/types/rbac";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { usePermissions } from "@/hooks/use-permissions";

function AccessDeniedRedirect() {
  const router = useRouter();
  useEffect(() => { router.replace("/"); }, [router]);
  return null;
}

export default function RegisterNodePage() {
  return (
    <RequireRole role={Role.PARTNER} fallback={<AccessDeniedRedirect />}>
      <ReadOnlyBanner />
      <NodeRegisterForm />
    </RequireRole>
  );
}
