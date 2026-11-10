"use client";

/**
 * Page-level role gate.
 *
 * important: this redirects only on a decision it can positively make: the session
 * resolved as authenticated and the role is absent. A session that momentarily reads
 * as unauthenticated (ex; a wake from sleep where /api/auth/session fails) must not
 * throw the operator off the page they were on.
 *
 * UI only. Enforcement is the authorized() callback in auth.ts and the backend.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { usePermissions } from "./use-permissions";
import { Role } from "@/types/rbac";

export interface UseRequireRoleResult {
  /** User holds the required role */
  allowed: boolean;
  /** A definitive answer exists: render the page or the denial */
  isResolved: boolean;
}


export function useRequireRole(role: Role, redirectTo: string = "/"): UseRequireRoleResult {
  const router = useRouter();
  const { hasRole, isLoading, isAuthenticated } = usePermissions();

  const allowed = hasRole(role);

  // important: rendering and redirecting are different questions. The page renders
  // as soon as roles are readable, which stays true through a background session
  // refresh; gating render on isAuthenticated would blank the page every time
  // useSession().update() briefly flips status back to "loading".
  const isResolved = !isLoading;

  useEffect(() => {
    // Redirect only on a known denial: authenticated, and the role absent.
    //
    // important: a session that momentarily reads as unauthenticated must NOT
    // navigate. A 401 storm makes useSession().update() produce exactly that
    // reading. A dead session is surfaced in place by the re-auth banner that
    // api.ts raises through onAuthFailure.
    if (isAuthenticated && !allowed) {
      router.replace(redirectTo);
    }
  }, [isAuthenticated, allowed, redirectTo, router]);

  return { allowed, isResolved };
}
