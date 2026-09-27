/**
 * NextAuth.js v5 Configuration for Authentik OAuth2/OIDC
 *
 * This configuration provides SSO authentication
 * using Authentik as the Identity Provider.
 */

import NextAuth from "next-auth";
import type { NextAuthConfig, User, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";
import { callbackPath, redirectTo } from "@/lib/auth-redirect";

// Extend the built-in types to include custom fields
declare module "next-auth" {
  interface Session {
    user: {
      id: string;
      name?: string | null;
      email?: string | null;
      image?: string | null;
      groups?: string[];
      roles?: string[];
    };
    accessToken?: string;
    error?: string;
  }

  interface User {
    id: string;
    name?: string | null;
    email?: string | null;
    image?: string | null;
    groups?: string[];
    roles?: string[];
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    accessToken?: string;
    refreshToken?: string;
    expiresAt?: number;
    error?: string;
    groups?: string[];
    roles?: string[];
    authentikId?: string; // Persistent Authentik user ID (profile.sub)
    // Unix seconds. Earliest a failed refresh may be retried. Kept separate from expiresAt
    // so the cooldown never has to lie about when the access token actually expires.
    retryAfter?: number;
  }
}

// Authentik OAuth2 Provider configuration
const authentikProvider = {
  id: "authentik",
  name: "Authentik",
  type: "oidc" as const,
  issuer: process.env.AUTHENTIK_ISSUER,
  clientId: process.env.AUTHENTIK_CLIENT_ID,
  clientSecret: process.env.AUTHENTIK_CLIENT_SECRET,
  authorization: {
    params: {
      scope: "openid profile email offline_access",
    },
  },
  // Map Authentik user info to NextAuth user object
  profile(profile: any) {
    // Resolve email: use profile.email, fallback to preferred_username (if it looks like email), then sub
    const resolvedEmail = profile.email || profile.preferred_username || "";
    // Resolve ID: must be non-empty for NextAuth to accept the account
    const resolvedId = resolvedEmail || profile.sub;
    
    return {
      // Use email as primary ID, fallback to preferred_username or sub
      id: resolvedId,
      name: profile.name || profile.preferred_username,
      email: resolvedEmail,
      image: profile.picture,
      groups: profile.groups || [],
      roles: profile.groups?.filter((g: string) => g.startsWith("role_")) || [],
      // Store original Authentik sub for reference
      authentikSub: profile.sub,
    };
  },
};

// Cooldown period (seconds) before retrying a failed token refresh.
// Prevents flooding the IdP with requests when it's unreachable.
const TOKEN_REFRESH_COOLDOWN_SECONDS = 60;

// important: the IdP is remote (AUTHENTIK_ISSUER resolves to a public host), so a refresh is
// a real internet round trip: measured 1.27s warm and 2.05s when discovery is uncached. The
// request path gets a budget just above the worst measurement, because a navigation that
// waits longer than that has already failed as far as the operator is concerned. Background
// refreshes, which nobody is watching, keep the generous budget.
const IDP_TIMEOUT_REQUEST_MS = 3_000;
const IDP_TIMEOUT_BACKGROUND_MS = 10_000;

// note: middleware compiles to the edge runtime, the /api/auth/session poll to nodejs. That
// is the only reliable signal for "is a paint waiting on this refresh".
const ON_REQUEST_PATH = process.env.NEXT_RUNTIME === "edge";
const IDP_TIMEOUT_MS = ON_REQUEST_PATH ? IDP_TIMEOUT_REQUEST_MS : IDP_TIMEOUT_BACKGROUND_MS;

// Refresh this many seconds BEFORE expiry. Deliberately asymmetric: Authentik access tokens
// live ~5 min and SessionProvider polls /api/auth/session every 4 min (auth-provider.tsx), so
// at every poll the token has ~60s left. A 120s background window guarantees the poll is what
// spends the IdP round trip. The request path uses 0, so middleware refreshes only when the
// token is genuinely expired, which now means only when the poll was missed (sleep, new tab).
const REFRESH_SKEW_BACKGROUND_S = 120;
const REFRESH_SKEW_REQUEST_S = 0;
const REFRESH_SKEW_S = ON_REQUEST_PATH ? REFRESH_SKEW_REQUEST_S : REFRESH_SKEW_BACKGROUND_S;

// Cached OIDC token endpoint to avoid re-fetching discovery on every refresh
let _cachedTokenEndpoint: string | null = null;

async function resolveTokenEndpoint(signal: AbortSignal): Promise<string> {
  if (_cachedTokenEndpoint) return _cachedTokenEndpoint;

  const issuer = process.env.AUTHENTIK_ISSUER;
  if (!issuer) throw new Error("AUTHENTIK_ISSUER not configured");

  try {
    const response = await fetch(`${issuer}.well-known/openid-configuration`, { signal });
    if (!response.ok) throw new Error(`discovery HTTP ${response.status}`);
    const discovery: { token_endpoint?: string } = await response.json();
    if (!discovery.token_endpoint) throw new Error("discovery returned no token_endpoint");
    _cachedTokenEndpoint = discovery.token_endpoint;
    return _cachedTokenEndpoint;
  } catch (error) {
    // important: only a discovery failure invalidates the cached endpoint. Clearing it on a
    // failed grant made the next refresh pay a second round trip, 2.05s instead of 1.27s.
    _cachedTokenEndpoint = null;
    throw error;
  }
}

// Token refresh function for handling expired tokens.
// important: this never rejects. Every failure is folded into the returned JWT, which is what
// lets the dedupe wrapper below share one promise without risking an unhandled rejection.
async function refreshAccessToken(token: JWT): Promise<JWT> {
  const nowSeconds = Math.floor(Date.now() / 1000);
  try {
    // One deadline shared by discovery and the grant, so the worst case is the budget rather
    // than twice the budget.
    const signal = AbortSignal.timeout(IDP_TIMEOUT_MS);
    const tokenEndpoint = await resolveTokenEndpoint(signal);

    const response = await fetch(tokenEndpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({
        client_id: process.env.AUTHENTIK_CLIENT_ID || "",
        client_secret: process.env.AUTHENTIK_CLIENT_SECRET || "",
        grant_type: "refresh_token",
        refresh_token: token.refreshToken || "",
      }),
      signal,
    });

    const tokens = await response.json();

    if (!response.ok) {
      throw tokens;
    }

    return {
      ...token,
      accessToken: tokens.access_token,
      expiresAt: Math.floor(Date.now() / 1000) + tokens.expires_in,
      refreshToken: tokens.refresh_token ?? token.refreshToken,
      error: undefined,
      retryAfter: undefined,
    };
  } catch (error) {
    // Compact log - avoid dumping the full stack on every request
    const reason = error instanceof Error ? error.message : String(error);
    console.warn(`[AUTH] Token refresh failed: ${reason}`);

    return {
      ...token,
      // important: the request path must not be able to mark the session errored. It is the
      // high-concurrency path, and `error` raises a destructive re-login banner in
      // auth-provider.tsx. A navigation that cannot reach the IdP should render the page with
      // the token it already has: nothing on the server reads the access token, so a stale one
      // changes nothing about what renders. Recovery runs through the 401 handler in lib/api.ts
      // and the background poll, which both keep their ability to raise the banner.
      ...(ON_REQUEST_PATH ? {} : { error: "RefreshAccessTokenError" }),
      // Cooldown lives here, not in expiresAt, so expiresAt stays truthful.
      retryAfter: nowSeconds + TOKEN_REFRESH_COOLDOWN_SECONDS,
    };
  }
}

// In-flight refresh dedupe.
// note: per-runtime and per-process. Middleware (edge) and /api/auth/session (nodejs) are
// separate module instances, so this bounds a burst at one grant per runtime, not one
// globally. Losing the map only costs extra grants, it can never produce a wrong token.
const _inFlightRefresh = new Map<string, { promise: Promise<JWT>; joined: number }>();

function refreshKey(token: JWT): string {
  // note: identity of the grant, not a secret to log. The suffix changes the moment the IdP
  // rotates the refresh token, which is exactly when a new grant becomes legitimate.
  return `${token.authentikId ?? token.sub ?? "?"}:${(token.refreshToken ?? "").slice(-24)}`;
}

function refreshAccessTokenDeduped(token: JWT): Promise<JWT> {
  const key = refreshKey(token);
  const inFlight = _inFlightRefresh.get(key);
  if (inFlight) {
    inFlight.joined += 1;
    return inFlight.promise;
  }

  const startedAt = Date.now();
  const promise = refreshAccessToken(token);
  _inFlightRefresh.set(key, { promise, joined: 0 });

  void promise.finally(() => {
    const entry = _inFlightRefresh.get(key);
    _inFlightRefresh.delete(key);
    // Deliberate and permanent: the only way to prove from outside that a burst of concurrent
    // requests produced exactly one IdP grant. Logs no token material.
    console.info(
      `[AUTH] refresh grant runtime=${process.env.NEXT_RUNTIME ?? "nodejs"} ` +
        `shared=${(entry?.joined ?? 0) + 1} ms=${Date.now() - startedAt}`
    );
  });

  return promise;
}

type JwtParams = Parameters<NonNullable<NonNullable<NextAuthConfig["callbacks"]>["jwt"]>>[0];

async function jwtCallback({ token, user, account }: JwtParams): Promise<JWT> {
  // Initial sign in
  if (account && user) {
    // Use EMAIL as the persistent user identifier.
    // This is more reliable than the Authentik sub, which can change per session.
    const persistentUserId = user.email || user.id;

    return {
      ...token,
      accessToken: account.access_token,
      refreshToken: account.refresh_token,
      expiresAt: account.expires_at,
      groups: (user as User).groups || [],
      roles: (user as User).roles || [],
      // CRITICAL: use email as the persistent user ID. The Authentik sub changes per
      // session, email is stable.
      authentikId: persistentUserId,
    };
  }

  const nowSeconds = Math.floor(Date.now() / 1000);

  // Still comfortably valid. On the background path the skew makes this the branch that hands
  // the IdP round trip to the session poll instead of to a navigation.
  if (token.expiresAt && nowSeconds < token.expiresAt - REFRESH_SKEW_S) {
    return token;
  }

  // A previous refresh failed. Wait out the cooldown rather than hammering the IdP.
  if (token.retryAfter && nowSeconds < token.retryAfter) {
    return token;
  }

  return await refreshAccessTokenDeduped(token);
}

// Only root-level public files may bypass the page authentication callback.
// A dotted page under /inventory, /console, or /terminal still needs its role gate.
const PUBLIC_ASSET_EXTENSION =
  /^\/[^/]+\.(?:js|mjs|css|map|ico|png|jpe?g|gif|svg|webp|avif|woff2?|ttf|otf|txt|webmanifest|wasm|json)$/i;

// NextAuth configuration
export const authConfig: NextAuthConfig = {
  providers: [authentikProvider],

  // Use JWT for session management (stateless)
  session: {
    strategy: "jwt",
    maxAge: 7 * 24 * 60 * 60, // 7 days
  },

  pages: {
    signIn: "/auth/signin",
    error: "/auth/error",
  },

  callbacks: {
    // Handle JWT token updates
    jwt: jwtCallback,

    // Customize session object
    async session({ session, token }): Promise<Session> {
      return {
        ...session,
        user: {
          ...session.user,
          // Use authentikId (persistent) instead of token.sub (session-based)
          id: token.authentikId || token.sub || "",
          groups: token.groups || [],
          roles: token.roles || [],
        },
        accessToken: token.accessToken,
        error: token.error,
      };
    },

    // Control which routes are authorized
    authorized({ auth, request }) {
      const { nextUrl } = request;
      const host = request.headers.get("host");
      const isLoggedIn = !!auth?.user;
      const isOnAuthPage = nextUrl.pathname === "/auth" || nextUrl.pathname.startsWith("/auth/");
      const isApiRoute = nextUrl.pathname === "/api" || nextUrl.pathname.startsWith("/api/");
      // Only known root-level public assets and API routes bypass the page gate.
      const isPublicAsset =
        nextUrl.pathname.startsWith("/_next/") ||
        PUBLIC_ASSET_EXTENSION.test(nextUrl.pathname);

      // Allow public assets and API routes
      if (isPublicAsset || isApiRoute) {
        return true;
      }

      // Redirect logged-in users away from auth pages
      if (isOnAuthPage) {
        if (isLoggedIn) {
          return Response.redirect(redirectTo(nextUrl, host, "/"));
        }
        return true; // Allow access to auth pages for non-logged-in users
      }

      // Require authentication for all other pages.
      // important: return a redirect Response, not `false`. next-auth v5 checks
      // `authorized instanceof Response` BEFORE the custom middleware function in middleware.ts, so a
      // bare `false` is shadowed and never fires the sign-in redirect - leaving /console and /terminal
      // to render tokenless and fail their WebSocket. The redirect drives /auth/signin -> silent SSO
      // (shared IdP) -> back with a token, which those pages need.
      // The redirect stays on the origin the request arrived on (see lib/auth-redirect.ts), so a
      // loopback liveness probe is never sent through the public edge.
      if (!isLoggedIn) {
        return Response.redirect(
          redirectTo(nextUrl, host, "/auth/signin", { callbackUrl: callbackPath(nextUrl) })
        );
      }

      // ---- Three-Tier Role-based route protection ----
      // Extract role groups from session
      const userGroups: string[] = (auth?.user as any)?.groups ?? [];
      const groupsLower = userGroups.map((g) => g.toLowerCase().trim());

      // resolve admin/partner using same mapping as rbac.ts
      const adminGroups = ["role_admin", "admin", "admins", "authentik admins", "orcastra-admins"];
      const partnerGroups = [...adminGroups, "role_partner", "partner", "orcastra-partners"];

      const isAdmin = groupsLower.some((g) => adminGroups.includes(g));
      const isPartner = groupsLower.some((g) => partnerGroups.includes(g));

      // Admin-only routes (Identities & Groups moved to Admin+Partner in sidebar,
      // but middleware also allows Partner)
      const adminOrPartnerPaths = [
        "/inventory/identities",
        "/inventory/groups",
        // Installing an add-on hands a third party a credential into an organization, so the
        // gate is a real redirect here rather than a component that renders null.
        "/extensions",
      ];

      const isAdminOrPartnerRoute = adminOrPartnerPaths.some(
        (p) => nextUrl.pathname === p || nextUrl.pathname.startsWith(p + "/")
      );

      if (isAdminOrPartnerRoute && !isPartner) {
        return Response.redirect(redirectTo(nextUrl, host, "/"));
      }

      return true;
    },
  },

  // Enable debug mode in development
  debug: process.env.NODE_ENV === "development",

  // Trust the Authentik HTTPS certificate
  // In production, ensure proper SSL configuration
  trustHost: true,
};

// Export NextAuth handlers and helpers.
// note: the eager form on purpose. The lazy form, NextAuth((request) => config), makes auth()
// return a promise of the middleware rather than the middleware, and the only reason to reach
// for it was to branch on the prefetch header, which middleware never receives (see
// middleware.ts).
export const { handlers, auth, signIn, signOut } = NextAuth(authConfig);
