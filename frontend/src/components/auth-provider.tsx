"use client";

/**
 * Auth Provider Component
 *
 * Wraps the application with SessionProvider for client-side session access.
 * Automatically syncs the Authentik access token into the API client
 * so all backend requests include a valid Bearer token.
 *
 * Key features:
 * - Proactive token refresh via SessionProvider refetchInterval (4 min)
 * - 401 handling: triggers session refresh instead of immediate redirect
 * - Gates children rendering until token is ready (prevents race conditions)
 */

import { SessionProvider, useSession, signIn } from "next-auth/react";
import { ReactNode, useEffect, useRef, useState, useCallback } from "react";
import { setAccessToken, onSessionRefreshNeeded, onAuthFailure, onAuthRecovered } from "@/lib/api";

// Session refresh interval (seconds) - refresh token before it expires.
// Authentik default access token lifetime is ~5 min, so refresh every 4 min.
const SESSION_REFRESH_INTERVAL_S = 4 * 60;

/**
 * Inner component that syncs session accessToken → API client.
 * Must be inside SessionProvider to use useSession().
 *
 * Gates children rendering until the session is resolved and the
 * access token is set, preventing React Query from firing requests
 * before the Bearer token is available (race condition → 401s).
 *
 * Also handles RefreshAccessTokenError - shows a re-login prompt
 * when the IdP is reachable, but avoids redirect loops when it's down.
 */
function TokenSync({ children }: { children: ReactNode }) {
  const { data: session, status, update: updateSession } = useSession();
  const [tokenReady, setTokenReady] = useState(false);
  const [authError, setAuthError] = useState(false);
  const retryCountRef = useRef(0);
  const wasAuthenticatedRef = useRef(false);
  const signOutConfirmedRef = useRef(false);

  // Register session refresh callback so api.ts can trigger re-auth on 401
  useEffect(() => {
    const refreshFn = async () => {
      try {
        await updateSession();
      } catch {
        // session update failed - will show error on next render
      }
    };
    onSessionRefreshNeeded(refreshFn);
  }, [updateSession]);

  // api.ts no longer redirects on repeated 401s - it asks for the banner instead,
  // so the operator keeps the page they were on and can re-auth from there.
  useEffect(() => {
    onAuthFailure(() => setAuthError(true));
    // cleared only when a request actually succeeds, never by an effect re-running
    onAuthRecovered(() => setAuthError(false));
  }, []);

  useEffect(() => {
    if (status === "authenticated" && session?.accessToken) {
      // Check if token refresh failed - the IdP may be unreachable
      if (session.error === "RefreshAccessTokenError") {
        retryCountRef.current += 1;

        // Don't auto-redirect - it causes visible "random refresh" to the user.
        // Let them continue with the stale token; show an error banner instead.
        // api.ts no longer redirects on a 401 either: it raises the same banner.
        console.warn("[Auth] Token refresh failed, using stale token");
        setAuthError(true);
        setAccessToken(session.accessToken); // stale but non-null
        setTokenReady(true);
        return;
      }

      // Successful token - reset counters
      retryCountRef.current = 0;
      wasAuthenticatedRef.current = true;
      signOutConfirmedRef.current = false;
      // important: clear the banner here too. This branch raises it above without going
      // through api.ts, so onAuthRecovered never fires for it, and the destructive bar
      // stayed pinned to the top of the viewport for the life of the tab after the
      // session had fully recovered.
      setAuthError(false);
      setAccessToken(session.accessToken);
      setTokenReady(true);
    } else if (status === "unauthenticated") {
      // important: next-auth's fetchData() returns null on ANY failure, including a
      // transient network error, and SessionProvider commits that null verdict. Once
      // it does, _getSession short-circuits on the null session and the refetch poll
      // is gated behind it, so the tab stays wedged as signed-out until a reload.
      // A laptop waking before its wifi is enough to trigger it. Confirm once before
      // believing an unauthenticated verdict we did not previously hold.
      if (wasAuthenticatedRef.current && !signOutConfirmedRef.current) {
        signOutConfirmedRef.current = true;
        void (async () => {
          const confirmed = await updateSession().catch(() => null);
          if (!confirmed) {
            setAccessToken(null);
            setTokenReady(true);
          }
        })();
        return;
      }
      setAccessToken(null);
      setTokenReady(true); // resolved - no token needed
    }
  }, [session, status, updateSession]);

  // don't render children (or React Query hooks) until token is set
  if (!tokenReady) {
    return null;
  }

  return (
    <>
      {authError && (
        <div className="fixed top-0 inset-x-0 z-[100] bg-destructive/90 text-destructive-foreground px-4 py-2.5 text-sm flex items-center justify-center gap-3">
          <span>Your session could not be refreshed. Sign in again to continue.</span>
          <button
            onClick={() => signIn("authentik")}
            className="underline font-medium hover:opacity-80"
          >
            Retry Login
          </button>
        </div>
      )}
      {children}
    </>
  );
}

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  return (
    <SessionProvider
      refetchInterval={SESSION_REFRESH_INTERVAL_S}
      // the 4-minute poll already keeps the token inside Authentik's ~5-minute
      // lifetime. Refetching on tab focus adds nothing and is actively harmful:
      // it is the entry point to the null-session wedge described in TokenSync.
      refetchOnWindowFocus={false}
      // and do not poll while offline, for the same reason
      refetchWhenOffline={false}
    >
      <TokenSync>{children}</TokenSync>
    </SessionProvider>
  );
}
