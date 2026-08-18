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

/** Build a sign-in URL that carries where the operator actually was. */
function signInWithReturnPath(): string {
  if (typeof window === "undefined") return "/auth/signin";
  const url = new URL("/auth/signin", window.location.origin);
  url.searchParams.set("callbackUrl", window.location.href);
  return url.pathname + url.search;
}

export function useRequireRole(role: Role, redirectTo: string = "/"): UseRequireRoleResult {
  const router = useRouter();
  const { hasRole, isLoading, isAuthenticated } = usePermissions();

  const allowed = hasRole(role);

  // Three states, not two. "Still loading" and "signed out" both fail an
  // isAuthenticated check but need opposite handling, and collapsing them leaves
  // a signed-out operator on a permanently blank page: no loader, no content, no
  // redirect, because the role check quietly returns false for an empty session.
  const isSignedOut = !isLoading && !isAuthenticated;

  // important: renderability and redirectability are different questions. We
  // render as soon as roles are readable, which stays true through a background
  // session refresh - gating render on isAuthenticated would blank the page every
  // time useSession().update() briefly flips status back to "loading".
  const isResolved = !isLoading && !isSignedOut;

  useEffect(() => {
    if (isSignedOut) {
      // The session is genuinely gone (auth-provider already confirmed it once
      // before clearing). Send them to sign-in carrying the current URL, so
      // silent SSO returns them to this page rather than to the home page. That
      // return path is the whole point of this change.
      router.replace(signInWithReturnPath());
      return;
    }
    // Otherwise only redirect on a denial we positively know: authenticated,
    // and the role genuinely absent.
    if (isAuthenticated && !allowed) {
      router.replace(redirectTo);
    }
  }, [isSignedOut, isAuthenticated, allowed, redirectTo, router]);

  return { allowed, isResolved };
}
