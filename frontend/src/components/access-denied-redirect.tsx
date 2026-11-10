"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

interface AccessDeniedRedirectProps {
  /** Where to send a user who does not hold the required role. */
  to: string;
}

// Fallback for a page-level RequireRole. Renders nothing and redirects, so the operator
// lands on a page they can use instead of an empty screen.
export function AccessDeniedRedirect({ to }: AccessDeniedRedirectProps) {
  const router = useRouter();
  useEffect(() => {
    router.replace(to);
  }, [router, to]);
  return null;
}
