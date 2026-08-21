"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

interface AccessDeniedRedirectProps {
  /** Where to send a user who does not hold the required role. */
  to: string;
}

// Fallback for a page-level RequireRole. Renders nothing and bounces, so the operator lands
// somewhere they can actually use instead of on an empty screen.
export function AccessDeniedRedirect({ to }: AccessDeniedRedirectProps) {
  const router = useRouter();
  useEffect(() => {
    router.replace(to);
  }, [router, to]);
  return null;
}
