"use client";

/**
 * useRequireRole - page-level role gate.
 *
 * important: this only redirects on a decision it can positively make, meaning the
 * session resolved as authenticated and the role is genuinely absent. A session that
 * momentarily reads as unauthenticated - a wake-from-sleep where /api/auth/session
 * fails, for instance - must not throw the operator off the page they were on. That
 * bounce was the reported "I came back from AFK and I'm on the home page" bug.
 *
 * NOTE: UI only. Real enforcement is the authorized() callback in auth.ts and the
 * backend, both of which are independent of this hook.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { usePermissions } from "./use-permissions";
import { Role } from "@/types/rbac";

export interface UseRequireRoleResult {
  /** User holds the required role */
  allowed: boolean;
  /** A definitive answer exists - render the page or the denial, not a guess */
  isResolved: boolean;
}

export function useRequireRole(role: Role, redirectTo: string = "/"): UseRequireRoleResult {
  const router = useRouter();
  const { hasRole, isLoading, isAuthenticated } = usePermissions();

  const allowed = hasRole(role);
  // important: renderability and redirectability are different questions. We can
  // render as soon as roles are readable, which stays true through a background
  // session refresh - gating render on isAuthenticated would blank the page every
  // time useSession().update() briefly flips status back to "loading".
  const isResolved = !isLoading;

  useEffect(() => {
    // ...but only ever redirect on a denial we positively know: authenticated,
    // and the role genuinely absent.
    if (isAuthenticated && !allowed) {
      router.replace(redirectTo);
    }
  }, [isAuthenticated, allowed, redirectTo, router]);

  return { allowed, isResolved };
}
