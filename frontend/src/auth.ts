/**
 * NextAuth.js v5 Configuration for Authentik OAuth2/OIDC
 *
 * This configuration provides SSO authentication
 * using Authentik as the Identity Provider.
 */

import NextAuth from "next-auth";
import type { NextAuthConfig, User, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";
import { callbackPath, redirectTo, safeCallbackPath } from "@/lib/auth-redirect";
import { readGroupsClaim, roleGroups } from "@/lib/token-claims";
import { forget, grantOnce, holdTombstones, upgradeSpent, type GrantOutcome, type RefreshedFields } from "@/lib/refresh-grants";

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
    // Unix seconds the groups were last read from an issued access token.
    groupsReadAt?: number;
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

// important: only /api/auth/session (nodejs) refreshes; middleware runs its own NextAuth
// instance that never does (middleware.ts). With an IdP that rotates refresh tokens, two writers
// spend the same token and the second one is refused, which ends the session.
// important: a session read waits for its grant to finish, up to GRANT_CAP_MS. Giving up earlier
// would discard a rotation the IdP may already have committed; the new refresh token exists only
// in that answer. Measured 1.27 s warm, 2.05 s with discovery uncached.
const GRANT_CAP_MS = 20_000;

// Refresh this long before expiry. Authentik access tokens live ~5 min and SessionProvider polls
// /api/auth/session every 4 min (auth-provider.tsx), so at every poll the token has ~60 s left
// and the poll is what spends the IdP round trip, never a navigation.
const REFRESH_SKEW_S = 120;

// Cached OIDC discovery to avoid re-fetching it on every refresh
interface Discovery {
  token_endpoint: string;
  revocation_endpoint?: string;
}
let _cachedDiscovery: Discovery | null = null;

async function resolveDiscovery(signal: AbortSignal): Promise<Discovery> {
  if (_cachedDiscovery) return _cachedDiscovery;

  const issuer = process.env.AUTHENTIK_ISSUER;
  if (!issuer) throw new Error("AUTHENTIK_ISSUER not configured");

  try {
    const response = await fetch(`${issuer}.well-known/openid-configuration`, { signal });
    if (!response.ok) throw new Error(`discovery HTTP ${response.status}`);
    const discovery: Partial<Discovery> = await response.json();
    if (!discovery.token_endpoint) throw new Error("discovery returned no token_endpoint");
    _cachedDiscovery = { token_endpoint: discovery.token_endpoint, revocation_endpoint: discovery.revocation_endpoint };
    return _cachedDiscovery;
  } catch (error) {
    // important: only a discovery failure invalidates the cache. Clearing it on a failed grant
    // made the next refresh pay a second round trip, 2.05s instead of 1.27s.
    _cachedDiscovery = null;
    throw error;
  }
}

// One refresh_token grant. important: never rejects; a failure is an outcome, so every caller
// sharing the grant in lib/refresh-grants.ts sees the same answer.
async function grantTokens(refreshToken: string): Promise<GrantOutcome> {
  const nowSeconds = Math.floor(Date.now() / 1000);
  try {
    // One deadline shared by discovery and the grant.
    const signal = AbortSignal.timeout(GRANT_CAP_MS);
    const { token_endpoint: tokenEndpoint } = await resolveDiscovery(signal);
    const response = await fetch(tokenEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: process.env.AUTHENTIK_CLIENT_ID || "",
        client_secret: process.env.AUTHENTIK_CLIENT_SECRET || "",
        grant_type: "refresh_token",
        refresh_token: refreshToken,
      }),
      signal,
    });
    const tokens = await response.json();
    if (!response.ok) throw new Error(typeof tokens?.error === "string" ? tokens.error : `HTTP ${response.status}`);

    // A role granted or removed in Authentik reaches the session here (issue #17). A token with
    // no readable claim keeps the groups it had: Authentik sends [] for "none", so a missing
    // claim is a broken mapping, and /me/access still answers for the backend's own reading.
    const groups = readGroupsClaim(tokens.access_token);
    if (!groups) console.warn("[AUTH] Refreshed access token carried no groups claim; keeping the session's groups");
    return {
      ok: true,
      fields: {
        accessToken: tokens.access_token,
        expiresAt: Math.floor(Date.now() / 1000) + tokens.expires_in,
        refreshToken: tokens.refresh_token ?? refreshToken,
        // important: stamped on every successful grant, claim or not. It is the floor under the
        // notice's forced refresh, and a token without the claim must not lift it.
        groupsReadAt: nowSeconds,
        ...(groups ? { groups, roles: roleGroups(groups) } : {}),
      },
    };
  } catch (error) {
    console.warn(`[AUTH] Token refresh failed: ${error instanceof Error ? error.message : String(error)}`);
    return { ok: false };
  }
}

function withFields(token: JWT, fields: RefreshedFields): JWT {
  return { ...token, ...fields, error: undefined, retryAfter: undefined };
}

function failed(token: JWT): JWT {
  // Cooldown lives here, not in expiresAt, so expiresAt stays truthful.
  return { ...token, error: "RefreshAccessTokenError", retryAfter: Math.floor(Date.now() / 1000) + TOKEN_REFRESH_COOLDOWN_SECONDS };
}

async function refresh(token: JWT): Promise<JWT | null> {
  const refreshToken = token.refreshToken;
  if (!refreshToken) return failed(token);
  const { outcome } = await grantOnce(sessionSubject(token), refreshToken, async () => {
    const result = await grantTokens(refreshToken);
    // A grant whose token carried no groups claim keeps this session's groups, and so does every
    // reader the registry hands the result to.
    return result.ok && !result.fields.groups
      ? { ok: true, fields: { ...result.fields, groups: token.groups, roles: token.roles } }
      : result;
  });
  const settled = await outcome;
  if (!settled.ok && settled.forgotten) {
    // A rotation that landed after the sign-out: nobody will hold this token, so it goes too.
    if (settled.orphanedRefreshToken) void revokeAtIdp(settled.orphanedRefreshToken);
    return null;
  }
  if (!settled.ok) return failed(token);
  return withFields(token, settled.fields);
}

// RFC 7009: a signed-out refresh token is revoked at the IdP, so no copy of the cookie can use it.
// note: a 200 does not prove the token existed (RFC 7009 answers 200 for unknown tokens).
async function revokeAtIdp(refreshToken: string): Promise<boolean> {
  try {
    const signal = AbortSignal.timeout(5_000);
    const { revocation_endpoint: revocationEndpoint } = await resolveDiscovery(signal);
    if (!revocationEndpoint) {
      console.warn("[AUTH] the IdP advertises no revocation_endpoint; a signed-out refresh token stays valid there");
      return false;
    }
    const response = await fetch(revocationEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: process.env.AUTHENTIK_CLIENT_ID || "",
        client_secret: process.env.AUTHENTIK_CLIENT_SECRET || "",
        token: refreshToken,
        token_type_hint: "refresh_token",
      }),
      signal,
    });
    if (!response.ok) console.warn(`[AUTH] refresh token revocation answered ${response.status}`);
    return response.ok;
  } catch (error) {
    console.warn(`[AUTH] refresh token revocation failed: ${error instanceof Error ? error.message : String(error)}`);
    return false;
  }
}

// When the IdP could not revoke at sign-out, this process refuses the chain for a day and keeps
// trying; a sign-out never fails because of it.
const UNREVOKED_TOMBSTONE_MS = 24 * 60 * 60_000;
const REVOKE_RETRY_MS = [30_000, 5 * 60_000];

function retryRevocation(refreshToken: string, attempt = 0): void {
  if (attempt >= REVOKE_RETRY_MS.length) return;
  const timer = setTimeout(async () => {
    if (!(await revokeAtIdp(refreshToken))) retryRevocation(refreshToken, attempt + 1);
  }, REVOKE_RETRY_MS[attempt]);
  (timer as { unref?: () => void }).unref?.();
}

function sessionSubject(token: JWT): string {
  return token.authentikId ?? token.sub ?? "";
}

type JwtParams = Parameters<NonNullable<NonNullable<NextAuthConfig["callbacks"]>["jwt"]>>[0];

// The access notice's "Check again" may force one refresh, at most this often per session, so a
// held-down button cannot spend the rotating refresh token in a loop.
const ROLE_RECHECK_FLOOR_S = 20;

async function jwtCallback({ token, user, account, trigger, session }: JwtParams): Promise<JWT | null> {
  // Initial sign in
  if (account && user) {
    // Use EMAIL as the persistent user identifier.
    // This is more reliable than the Authentik sub, which can change per session.
    const persistentUserId = user.email || user.id;
    // The backend decides on the access token's claim, so the session starts from the same one.
    const groups = readGroupsClaim(account.access_token) ?? (user as User).groups ?? [];

    return {
      ...token,
      accessToken: account.access_token,
      refreshToken: account.refresh_token,
      expiresAt: account.expires_at,
      groups,
      roles: roleGroups(groups),
      groupsReadAt: Math.floor(Date.now() / 1000),
      // CRITICAL: use email as the persistent user ID. The Authentik sub changes per
      // session, email is stable.
      authentikId: persistentUserId,
    };
  }

  // A cookie whose refresh token was spent moments ago (a read that was already in flight when
  // the rotation landed, another tab) takes the newest tokens of its chain; the registry logs it.
  if (token.refreshToken) {
    const upgrade = await upgradeSpent(sessionSubject(token), token.refreshToken);
    if (upgrade) token = withFields(token, upgrade.fields);
  }

  const nowSeconds = Math.floor(Date.now() / 1000);
  const coolingDown = nowSeconds < (token.retryAfter ?? 0);

  // important: the client's update() payload is only a signal here. Nothing in it is copied into
  // the token; the groups come from the IdP's answer to the refresh grant.
  const recheckRoles =
    trigger === "update" &&
    (session as { refreshRoles?: unknown } | null | undefined)?.refreshRoles === true &&
    nowSeconds - (token.groupsReadAt ?? 0) >= ROLE_RECHECK_FLOOR_S &&
    !coolingDown;
  if (recheckRoles) return refresh(token);

  // Still comfortably valid: the session poll spends the round trip before it is needed.
  if (token.expiresAt && nowSeconds < token.expiresAt - REFRESH_SKEW_S) return token;

  // A previous refresh failed. Wait out the cooldown rather than hammering the IdP.
  if (coolingDown) return token;

  return refresh(token);
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

      // Redirect logged-in users away from auth pages, to the page the sign-in was for when it
      // names a safe one (a second tab, a bookmarked sign-in step), else home.
      if (isOnAuthPage) {
        if (isLoggedIn) {
          const target = safeCallbackPath(nextUrl.searchParams.get("callbackUrl"), nextUrl.origin) ?? "/";
          return Response.redirect(redirectTo(nextUrl, host, target));
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

  events: {
    // A cookie from before the sign-out must not be healed into the signed-out chain, and a grant
    // still in flight for it must not write the session back.
    async signOut(message) {
      const token = "token" in message ? message.token : null;
      if (!token?.refreshToken) return;
      const subject = sessionSubject(token);
      const head = (await upgradeSpent(subject, token.refreshToken, { log: false }))?.fields.refreshToken ?? token.refreshToken;
      const chain = await forget(subject, token.refreshToken);
      if (!(await revokeAtIdp(head))) {
        holdTombstones(chain, UNREVOKED_TOMBSTONE_MS);
        retryRevocation(head);
      }
    },
  },
};

// The route handlers: the one place that refreshes. Middleware and the layout's session read
// build their own non-refreshing instances from authConfig.
// note: the eager form on purpose. The lazy form, NextAuth((request) => config), returns promises
// of the handlers, and nothing here needs the request to build the config.
export const { handlers } = NextAuth(authConfig);
