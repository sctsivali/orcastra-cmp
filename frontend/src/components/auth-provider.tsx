"use client";

/**
 * Session provider that keeps the API client's bearer token in step with the next-auth session.
 *
 * The session is polled every 4 minutes, a 401 asks for a session refresh instead of
 * redirecting, and children render only once the token is installed. A session the IdP ended
 * signs in again on its own in a tab that never worked (ex; a deep link opened in a new tab),
 * and behind a Sign In Again banner in one that did.
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

// Polls inside Authentik's default access token lifetime of about 5 minutes.
const SESSION_REFRESH_INTERVAL_S = 4 * 60;

/**
 * Installs the session's access token in the API client. Must render inside SessionProvider.
 *
 * Children wait until the session is resolved and the token is set, so React Query never sends
 * a request without the bearer. A failed refresh keeps the stale token behind a sign-in banner
 * and never redirects, which would loop while the IdP is down.
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

  // Lets api.ts refresh the session on a 401.
  useEffect(() => {
    const refreshFn = async () => {
      try {
        await updateSession();
      } catch {
        // The failed session shows on the next render.
      }
    };
    onSessionRefreshNeeded(refreshFn);
    // A socket whose ticket was refused asks for a fresh token and needs to know what it got.
    // A token for the same actor is installed here, before the socket mints again; another
    // actor goes through the effect below, which remounts the page.
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

  // api.ts raises the banner on repeated 401s instead of redirecting, so the operator keeps the
  // page they were on and can sign in again from there.
  useEffect(() => {
    onAuthFailure(() => setAuthError(true));
    // Cleared only when a request succeeds, never by an effect re-running.
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

      // The refresh failed; the IdP may be unreachable.
      if (session.error === REFRESH_FAILED) {
        retryCountRef.current += 1;

        // No redirect, which reads as a random page reload. The page continues on the stale
        // token behind the same banner api.ts raises on a 401.
        console.warn("[Auth] Token refresh failed, using stale token");
        setAuthError(true);
        synchronizeAuth(session.accessToken, actorScope); // stale but non-null
        return;
      }

      retryCountRef.current = 0;
      wasAuthenticatedRef.current = true;
      signOutConfirmedRef.current = false;
      // important: clear the banner here too. The branch above raises it without going through
      // api.ts, so onAuthRecovered never fires for it and the bar would stay for the life of
      // the tab.
      setAuthError(false);
      setSessionEnd(null);
      noteSessionWorked();
      synchronizeAuth(session.accessToken, actorScope);
    } else if (status === "unauthenticated") {
      // important: next-auth's fetchData() returns null on any failure, including a transient
      // network error, and SessionProvider commits that null. _getSession then short-circuits
      // and the refetch poll is gated behind it, so the tab stays signed out until a reload.
      // Confirm once before accepting an unauthenticated verdict after an authenticated one.
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
      synchronizeAuth(null, null); // resolved, no token needed
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

  // A session hydrated from the server skips next-auth's mount fetch, which refreshes a token
  // inside the 120 s background skew at load. Read it once in the background so the 4-minute poll
  // stays aligned with the token. Nothing waits on this read.
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
            Sign in again
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
      // The 4-minute poll already keeps the token inside Authentik's lifetime. A refetch on
      // tab focus can commit the null session described in TokenSync.
      refetchOnWindowFocus={false}
      // No poll while offline, for the same reason.
      refetchWhenOffline={false}
    >
      <TokenSync hydrated={session !== undefined}>{children}</TokenSync>
    </SessionProvider>
  );
}
