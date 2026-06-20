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
import { setAccessToken, reset401Counter, onSessionRefreshNeeded } from "@/lib/api";

// Session refresh interval (seconds) - refresh token before it expires.
// Authentik default access token lifetime is ~5 min, so refresh every 4 min.
const SESSION_REFRESH_INTERVAL_S = 4 * 60;

// Auth provider id used by signIn() - mode-aware so cert deployments re-auth via the cert flow.
const AUTH_PROVIDER_ID =
  process.env.NEXT_PUBLIC_AUTH_MODE === "client-cert" ? "client-cert" : "authentik";

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
  const reauthInFlightRef = useRef(false);

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

  useEffect(() => {
    // Client-cert mode: the short-lived token expired. Silently re-authenticate via the
    // client cert (the browser re-presents the imported cert to nginx, so signIn re-mints a
    // token with no user interaction). Guard so only one re-auth runs; on failure (cert gone
    // or invalid) fall back to the sign-in page. Handled BEFORE the accessToken check because
    // the expired token has been cleared (session.accessToken is now undefined).
    if (status === "authenticated" && session?.error === "SessionExpired") {
      setAccessToken(null);
      if (!reauthInFlightRef.current) {
        reauthInFlightRef.current = true;
        signIn(AUTH_PROVIDER_ID, { redirect: false })
          .then(async (res) => {
            if (res?.error) {
              window.location.href = "/api/auth/signin";
            } else {
              await updateSession();
            }
          })
          .catch(() => {
            window.location.href = "/api/auth/signin";
          })
          .finally(() => {
            reauthInFlightRef.current = false;
          });
      }
      return;
    }

    if (status === "authenticated" && session?.accessToken) {
      // Check if token refresh failed - the IdP may be unreachable
      if (session.error === "RefreshAccessTokenError") {
        retryCountRef.current += 1;

        // Don't auto-redirect - it causes visible "random refresh" to the user.
        // Let them continue with the stale token; show an error banner instead.
        // API calls may 401, but the 401 handler has a threshold before redirect.
        console.warn("[Auth] Token refresh failed — using stale token");
        setAuthError(true);
        setAccessToken(session.accessToken); // stale but non-null
        reset401Counter(); // prevent 401 cascade while we show the banner
        setTokenReady(true);
        return;
      }

      // Successful token - reset counters
      retryCountRef.current = 0;
      setAuthError(false);
      setAccessToken(session.accessToken);
      reset401Counter();
      setTokenReady(true);
    } else if (status === "unauthenticated") {
      setAccessToken(null);
      setTokenReady(true); // resolved - no token needed
    }
  }, [session, status]);

  // don't render children (or React Query hooks) until token is set
  if (!tokenReady) {
    return null;
  }

  return (
    <>
      {authError && (
        <div className="fixed top-0 inset-x-0 z-[100] bg-destructive/90 text-destructive-foreground px-4 py-2.5 text-sm flex items-center justify-center gap-3">
          <span>Authentication service is unreachable. Some features may not work.</span>
          <button
            onClick={() => signIn(AUTH_PROVIDER_ID)}
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
    <SessionProvider refetchInterval={SESSION_REFRESH_INTERVAL_S}>
      <TokenSync>{children}</TokenSync>
    </SessionProvider>
  );
}
