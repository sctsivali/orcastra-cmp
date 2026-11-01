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
 * - A session the IdP ended signs in again: on its own in a tab that never worked (a deep link
 *   opened in a new tab), behind a Sign In Again banner in one that did
 */

import { SessionProvider, useSession, signIn, __NEXTAUTH } from "next-auth/react";
import type { Session } from "next-auth";
import { ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { getActorQueryScope } from "@/hooks/use-query-scope";
import {
  getAccessToken,
  isAuthActorSynchronized,
  isAuthContextSynchronized,
  onAuthFailure,
  onAuthRecovered,
  onForceSessionRefresh,
  onSessionRefreshNeeded,
  setAccessToken,
} from "@/lib/api";
import { REFRESH_FAILED, REFRESH_TOKEN_REVOKED } from "@/lib/auth-refresh";
import { noteSessionWorked, reauthenticate } from "@/lib/reauth";
import { SOCKET_MESSAGES } from "@/lib/ws-close";

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
function TokenSync({ children, hydrated }: { children: ReactNode; hydrated: boolean }) {
  const { data: session, status, update: updateSession } = useSession();
  const alignedRef = useRef(false);
  const [synchronizedAuth, setSynchronizedAuth] = useState<
    { token: string | null; actorScope: string | null } | undefined
  >(undefined);
  const [authError, setAuthError] = useState(false);
  // "ended": the IdP refused the session's refresh token. "leaving": this tab is signing in again.
  const [sessionEnd, setSessionEnd] = useState<"ended" | "leaving" | null>(null);
  const retryCountRef = useRef(0);
  const wasAuthenticatedRef = useRef(false);
  const signOutConfirmedRef = useRef(false);
  const sessionToken = session?.accessToken ?? null;
  const actorScope = sessionToken ? getActorQueryScope(session) : null;

  const synchronizeAuth = useCallback((token: string | null, scope: string | null) => {
    // Install the module-global bearer before recording this context as ready. An actor change
    // keeps descendants withheld until the following render; a silent rotation for the same
    // actor leaves their forms and sockets mounted while this effect replaces the bearer.
    setAccessToken(token, scope);
    setSynchronizedAuth((current) =>
      current?.token === token && current.actorScope === scope
        ? current
        : { token, actorScope: scope },
    );
  }, []);

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
    // A socket whose ticket was refused asks for a fresh token and needs to know what it got.
    // note: the new token is installed here, before the socket mints again, when it belongs to
    // the same actor; another actor goes through the effect below, which remounts the page.
    onForceSessionRefresh(async () => {
      const before = getAccessToken();
      const next = await updateSession({ forceRefresh: true }).catch(() => null);
      if (!next) return "unavailable";
      if (next.error === REFRESH_TOKEN_REVOKED) return "revoked";
      if (next.error === REFRESH_FAILED || !next.accessToken) return "unavailable";
      if (next.accessToken === before) return "unchanged";
      const scope = getActorQueryScope(next);
      if (isAuthActorSynchronized(scope)) setAccessToken(next.accessToken, scope);
      return "refreshed";
    });
  }, [updateSession]);

  // api.ts no longer redirects on repeated 401s - it asks for the banner instead,
  // so the operator keeps the page they were on and can re-auth from there.
  useEffect(() => {
    onAuthFailure(() => setAuthError(true));
    // cleared only when a request actually succeeds, never by an effect re-running
    onAuthRecovered(() => setAuthError(false));
  }, []);

  useEffect(() => {
    let cancelled = false;

    if (status === "authenticated" && session?.accessToken) {
      // The IdP refused the refresh token: this session is over. A tab that never worked (a deep
      // link in a new tab) has nothing to lose and signs in again at once, which is silent while
      // the IdP session lives; reauthenticate holds a second automatic try within a minute back.
      // A tab that was working keeps its page and its open sessions until the operator chooses.
      if (session.error === REFRESH_TOKEN_REVOKED) {
        const onAuthPage = window.location.pathname === "/auth" || window.location.pathname.startsWith("/auth/");
        if (!wasAuthenticatedRef.current && !onAuthPage && reauthenticate({ auto: true })) {
          setSessionEnd("leaving");
          return;
        }
        console.warn("[Auth] the identity provider ended this session; offering to sign in again");
        setSessionEnd("ended");
        synchronizeAuth(session.accessToken, actorScope); // stale, so the page keeps its identity
        return;
      }

      // Check if token refresh failed - the IdP may be unreachable
      if (session.error === REFRESH_FAILED) {
        retryCountRef.current += 1;

        // Don't auto-redirect - it causes visible "random refresh" to the user.
        // Let them continue with the stale token; show an error banner instead.
        // api.ts no longer redirects on a 401 either: it raises the same banner.
        console.warn("[Auth] Token refresh failed, using stale token");
        setAuthError(true);
        synchronizeAuth(session.accessToken, actorScope); // stale but non-null
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
      setSessionEnd(null);
      noteSessionWorked();
      synchronizeAuth(session.accessToken, actorScope);
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
          if (!cancelled && !confirmed) {
            synchronizeAuth(null, null);
          }
        })();
        return () => {
          cancelled = true;
        };
      }
      synchronizeAuth(null, null); // resolved - no token needed
    }

    return () => {
      cancelled = true;
    };
  }, [actorScope, session, status, synchronizeAuth, updateSession]);

  const retainedSessionIsLoading = status === "loading" && sessionToken !== null;
  const authenticatedContextReady =
    (status === "authenticated" || retainedSessionIsLoading) &&
    sessionToken !== null &&
    synchronizedAuth?.actorScope === actorScope &&
    actorScope !== null &&
    isAuthActorSynchronized(actorScope);
  const unauthenticatedContextReady =
    status === "unauthenticated" &&
    synchronizedAuth?.token === null &&
    synchronizedAuth?.actorScope === null &&
    isAuthContextSynchronized(null, null);
  const authContextReady = authenticatedContextReady || unauthenticatedContextReady;

  // A session hydrated from the server skips next-auth's mount fetch, which is also what used to
  // refresh a token inside the 120 s background skew at load. Read it once, in the background, so
  // the token is in the same state it always was after a load and the 4-minute poll stays aligned
  // with it. Nothing waits on this read.
  //
  // important: gated on authenticatedContextReady, which is first true on the second commit.
  // Effects run child first, so on the first commit SessionProvider has not yet installed
  // __NEXTAUTH._getSession and calling it would hit the no-op it starts as. The "storage" event is
  // what next-auth itself fires after signIn and signOut: one GET, no loading flip, no broadcast.
  useEffect(() => {
    if (!hydrated || alignedRef.current || !authenticatedContextReady) return;
    alignedRef.current = true;
    void __NEXTAUTH._getSession({ event: "storage" });
  }, [hydrated, authenticatedContextReady]);

  // A tab that never worked is on its way to the IdP; it renders nothing that would ask for data.
  if (sessionEnd === "leaving") {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-background px-6 text-center" role="status" aria-live="polite">
        <p className="text-sm text-muted-foreground">{SOCKET_MESSAGES.signingInAgain}</p>
      </div>
    );
  }

  // Returning null commits an unmount only on initial resolution, sign-out, or a direct
  // authenticated A -> B transition. The effect then installs B and triggers a clean remount;
  // silent token rotation for the same actor never crosses this gate.
  if (!authContextReady) {
    return null;
  }

  return (
    <>
      {sessionEnd === "ended" ? (
        <div className="fixed top-0 inset-x-0 z-[100] bg-destructive/90 text-destructive-foreground px-4 py-2.5 text-sm flex items-center justify-center gap-3" role="alert">
          <span>Your session ended. Sign in again to continue.</span>
          <button
            type="button"
            onClick={() => reauthenticate({ auto: false })}
            className="underline font-medium hover:opacity-80"
          >
            Sign In Again
          </button>
        </div>
      ) : authError && (
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
  /** A session read on the server (auth-session.ts), or undefined to fetch one. Never null. */
  session?: Session;
}

export function AuthProvider({ children, session }: AuthProviderProps) {
  return (
    <SessionProvider
      session={session}
      refetchInterval={SESSION_REFRESH_INTERVAL_S}
      // the 4-minute poll already keeps the token inside Authentik's ~5-minute
      // lifetime. Refetching on tab focus adds nothing and is actively harmful:
      // it is the entry point to the null-session wedge described in TokenSync.
      refetchOnWindowFocus={false}
      // and do not poll while offline, for the same reason
      refetchWhenOffline={false}
    >
      <TokenSync hydrated={session !== undefined}>{children}</TokenSync>
    </SessionProvider>
  );
}
