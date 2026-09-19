"use client";

import { useSession } from "next-auth/react";

type ScopedSessionUser = {
  id?: string;
  email?: string | null;
  groups?: string[];
};

type ScopedSession = {
  user?: ScopedSessionUser;
  expires?: string;
  accessToken?: string;
} | null | undefined;

/**
 * Stable, non-secret browser-cache scope for the authenticated actor.
 *
 * React Query survives route changes and sign-out/sign-in transitions. Any response whose
 * contents depend on RBAC therefore needs the actor in its key, or the next user can briefly
 * receive the previous user's last-good data or terminal error from the in-memory cache.
 */
export function getActorQueryScope(session: ScopedSession): string {
  const user = session?.user;
  const groups = Array.isArray(user?.groups) ? [...user.groups].sort().join(",") : "";
  const identity = user?.id ?? user?.email ?? `session:${session?.expires ?? "anonymous"}`;
  return `${identity}:${groups}`;
}

export function useActorQueryScope() {
  const { data: session, status } = useSession();

  return {
    scopeKey: getActorQueryScope(session),
    accessToken: session?.accessToken ?? null,
    // A silent session refresh temporarily reports loading while retaining the current session.
    // Keep active reads mounted in that state, but never start them before a session exists.
    isAuthenticated: Boolean(session) && status !== "unauthenticated",
  };
}
