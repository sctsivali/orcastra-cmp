/**
 * NextAuth v5 configuration for single sign-on through Authentik over OIDC.
 */

import NextAuth from "next-auth";
import type { NextAuthConfig, User, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";
import { callbackPath, redirectTo, safeCallbackPath } from "@/lib/auth-redirect";
import { readGroupsClaim, roleGroups } from "@/lib/token-claims";
import { Role, hasMappedRole, hasMinimumRole, resolveRoles } from "@/types/rbac";
import { forget, grantOnce, holdTombstones, upgradeSpent, type GrantOutcome, type RefreshedFields } from "@/lib/refresh-grants";
import { REFRESH_FAILED, REFRESH_TOKEN_REVOKED, createIdpClient } from "@/lib/auth-refresh";
import { tokenStamp } from "@/lib/token-stamp";
import { pinnedSessionCookies } from "@/lib/session-cookie";
import { editionAuth } from "@/lib/edition-auth";
import { EDITION_ADMIN_ONLY_PATHS, EDITION_ADMIN_OR_PARTNER_PATHS } from "@/lib/edition";

// The custom fields this app keeps on the session, user and JWT.
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
    // A stamp of the access token, which changes when it rotates (lib/token-stamp.ts). The token
    // itself stays in the cookie and is attached by the /api/v1 proxy.
    tokenStamp?: string;
    // Unix seconds the session's token was granted, so a client holding several copies of the
    // session (another tab, a test harness) can tell which is newest. Not a secret.
    tokenIssuedAt?: number;
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
    authentikId?: string; // Persistent user id: the email, else the provider's user id.
    // Unix seconds. Earliest a failed refresh may be retried. Kept separate from expiresAt
    // so expiresAt stays the access token's real expiry.
    retryAfter?: number;
    // Unix seconds the groups were last read from an issued access token. Stamped by every
    // successful grant and at sign-in, so it is also when the session last refreshed.
    groupsReadAt?: number;
  }
}

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
  profile(profile: any) {
    // The email, else preferred_username.
    const resolvedEmail = profile.email || profile.preferred_username || "";
    // Must be non-empty for NextAuth to accept the account, so sub is the last fallback.
    const resolvedId = resolvedEmail || profile.sub;
    
    return {
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

// Seconds to wait before retrying a failed token refresh, so an unreachable IdP is not flooded.
const TOKEN_REFRESH_COOLDOWN_SECONDS = 60;

// important: only /api/auth/session (nodejs) refreshes; middleware runs its own NextAuth
// instance that never does (middleware.ts). With an IdP that rotates refresh tokens, two writers
// spend the same token and the second one is refused, which ends the session.
// important: a session read waits for its grant to finish, up to GRANT_CAP_MS. Giving up earlier
// would discard a rotation the IdP may already have committed; the new refresh token exists only
// in that answer. A grant normally takes one to two seconds.
const GRANT_CAP_MS = 20_000;

// Refresh this long before expiry. Authentik access tokens live ~5 min and SessionProvider polls
// /api/auth/session every 4 min (auth-provider.tsx), so at every poll the token has ~60 s left
// and the poll is what spends the IdP round trip, never a navigation.
const REFRESH_SKEW_S = 120;

// The route handlers run on nodejs. Middleware and the layout's session read never reach the IdP
// (they have their own non-refreshing NextAuth instances), so this is the only client.
const idp = createIdpClient({
  issuer: process.env.AUTHENTIK_ISSUER,
  clientId: process.env.AUTHENTIK_CLIENT_ID || "",
  clientSecret: process.env.AUTHENTIK_CLIENT_SECRET || "",
  timeoutMs: GRANT_CAP_MS,
  fetch: (input, init) => fetch(input, init),
  now: () => Date.now(),
  log: (level, line) => console[level](line),
  runtime: process.env.NEXT_RUNTIME ?? "nodejs",
});

// One refresh_token grant. important: never rejects; a failure is an outcome, so every caller
// sharing the grant in lib/refresh-grants.ts sees the same answer.
async function grantTokens(refreshToken: string): Promise<GrantOutcome> {
  const nowSeconds = Math.floor(Date.now() / 1000);
  const result = await idp.grant(refreshToken);
  if (!result.ok) return { ok: false, revoked: result.kind === "revoked" };
  const { accessToken, refreshToken: next, expiresIn } = result.tokens;
  // A role granted or removed in Authentik reaches the session here. A token with
  // no readable claim keeps the groups it had: Authentik sends [] for "none", so a missing
  // claim is a broken mapping, and /me/access still answers for the backend's own reading.
  const groups = readGroupsClaim(accessToken);
  if (!groups) console.warn("[AUTH] Refreshed access token carried no groups claim; keeping the session's groups");
  return {
    ok: true,
    fields: {
      accessToken,
      expiresAt: Math.floor(Date.now() / 1000) + expiresIn,
      refreshToken: next,
      // important: stamped on every successful grant, claim or not. It is the floor under the
      // notice's forced refresh, and a token without the claim must not lift it.
      groupsReadAt: nowSeconds,
      ...(groups ? { groups, roles: roleGroups(groups) } : {}),
    },
  };
}

function withFields(token: JWT, fields: RefreshedFields): JWT {
  return { ...token, ...fields, error: undefined, retryAfter: undefined };
}

function failed(token: JWT): JWT {
  // Cooldown lives here, not in expiresAt, so expiresAt stays truthful.
  return { ...token, error: REFRESH_FAILED, retryAfter: Math.floor(Date.now() / 1000) + TOKEN_REFRESH_COOLDOWN_SECONDS };
}

// important: the IdP refused the refresh token itself, so the session is over. The refresh token
// goes (nothing can use it) and the error tells middleware and the page to sign in again, which
// is silent while the IdP session lives. The stale access token stays so an open page keeps its
// identity until the operator chooses to sign in; the backend refuses it either way.
function revoked(token: JWT): JWT {
  return { ...token, refreshToken: undefined, retryAfter: undefined, error: REFRESH_TOKEN_REVOKED };
}

async function refresh(token: JWT): Promise<JWT | null> {
  // An edition that signs in without the identity provider renews its sessions its own way.
  if (editionAuth) return editionAuth.refresh(token);
  const refreshToken = token.refreshToken;
  if (!refreshToken) return revoked(token);
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
    // A rotation that landed after the sign-out: no session will hold this token, so revoke it.
    if (settled.orphanedRefreshToken) void revokeAtIdp(settled.orphanedRefreshToken);
    return null;
  }
  if (!settled.ok) return settled.revoked ? revoked(token) : failed(token);
  return withFields(token, settled.fields);
}

function revokeAtIdp(refreshToken: string): Promise<boolean> {
  return idp.revoke(refreshToken);
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
// A socket whose ticket came back 401 may force one refresh, at most this often per session.
const FORCE_REFRESH_FLOOR_S = 30;

async function jwtCallback({ token, user, account, trigger, session }: JwtParams): Promise<JWT | null> {
  // Initial sign in
  if (account && user) {
    if (editionAuth) return editionAuth.signedIn(token, user, account);
    // The email is the persistent user id; the Authentik sub can change per session.
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
      authentikId: persistentUserId,
      error: undefined,
      retryAfter: undefined,
    };
  }

  // important: only the nodejs route handlers refresh. Middleware runs on the edge with its own
  // instance whose jwt never refreshes (middleware.ts); this holds even if one ever reaches here.
  if (process.env.NEXT_RUNTIME === "edge") return token;

  // The IdP already refused this session's refresh token: nothing left to present.
  if (token.error === REFRESH_TOKEN_REVOKED) return token;

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
  const signal = trigger === "update" ? (session as { refreshRoles?: unknown; forceRefresh?: unknown } | null | undefined) : null;
  const sinceGrant = nowSeconds - (token.groupsReadAt ?? 0);
  const recheckRoles = signal?.refreshRoles === true && sinceGrant >= ROLE_RECHECK_FLOOR_S && !coolingDown;
  // A socket ticket refused with 401 although the session thought its token valid (a backend
  // clock ahead of this one, or a token the backend stopped accepting): one fresh grant settles it.
  const forced = signal?.forceRefresh === true && sinceGrant >= FORCE_REFRESH_FLOOR_S && !coolingDown;
  if (recheckRoles || forced) return refresh(token);

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

export const authConfig: NextAuthConfig = {
  providers: editionAuth?.providers ?? [authentikProvider],

  // On an https deployment the session cookie is `__Host-` (lib/session-cookie.ts), which the
  // /api/v1 proxy reads by the same rule. Other cookies keep NextAuth's own names.
  cookies: pinnedSessionCookies(process.env.AUTH_URL ?? process.env.NEXTAUTH_URL),

  // Use JWT for session management (stateless)
  session: {
    strategy: "jwt",
    maxAge: 7 * 24 * 60 * 60,
  },

  pages: {
    signIn: "/auth/signin",
    error: "/auth/error",
  },

  callbacks: {
    jwt: jwtCallback,

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
        // important: never the access token. Everything this returns reaches page JavaScript through
        // /api/auth/session and the layout's hydrated session.
        tokenStamp: await tokenStamp(token.accessToken),
        tokenIssuedAt: token.groupsReadAt,
        error: token.error,
      };
    },

    authorized({ auth, request }) {
      const { nextUrl } = request;
      const host = request.headers.get("host");
      // important: a session whose refresh token the IdP refused is over, whatever the cookie still
      // holds. Read as signed in, a deep link renders with an expired token and every socket ticket
      // comes back 401; read as signed out, it goes through /auth/signin, which is silent while the
      // IdP session lives, and comes back to the same link.
      const sessionEnded = auth?.error === REFRESH_TOKEN_REVOKED;
      const isLoggedIn = !!auth?.user && !sessionEnded;
      const isOnAuthPage = nextUrl.pathname === "/auth" || nextUrl.pathname.startsWith("/auth/");
      const isApiRoute = nextUrl.pathname === "/api" || nextUrl.pathname.startsWith("/api/");
      // Only known root-level public assets and API routes bypass the page gate.
      const isPublicAsset =
        nextUrl.pathname.startsWith("/_next/") ||
        PUBLIC_ASSET_EXTENSION.test(nextUrl.pathname);

      if (isPublicAsset || isApiRoute) {
        return true;
      }

      // Redirect logged-in users away from auth pages, to the page the sign-in was for when it
      // names a safe one (a second tab, a bookmarked sign-in step), else home. An ended session
      // is not logged in, so its sign-in page stays put and signs in again.
      if (isOnAuthPage) {
        if (isLoggedIn) {
          const target = safeCallbackPath(nextUrl.searchParams.get("callbackUrl"), nextUrl.origin) ?? "/";
          return Response.redirect(redirectTo(nextUrl, host, target));
        }
        return true;
      }

      // Every other page needs a session.
      // important: return a redirect Response, never `false`. next-auth v5 checks
      // `authorized instanceof Response` before the custom middleware function in middleware.ts, so a
      // bare `false` never fires the sign-in redirect, and /console and /terminal render without a
      // token. The redirect stays on the origin the request arrived on (lib/auth-redirect.ts), so a
      // loopback liveness probe is never sent through the public edge.
      if (!isLoggedIn) {
        return Response.redirect(
          redirectTo(nextUrl, host, "/auth/signin", { callbackUrl: callbackPath(nextUrl) })
        );
      }

      // Role-based route protection.
      const userGroups: string[] = (auth?.user as any)?.groups ?? [];

      // important: the same mapping useRequireRole and the backend use (types/rbac.ts, which also
      // reads role_<alias> groups). A separate list here would disagree with them and send home a
      // Partner the page and the API both accept.
      const roles = resolveRoles(userGroups);
      const isAdmin = hasMinimumRole(roles, Role.ADMIN);
      const isPartner = hasMinimumRole(roles, Role.PARTNER);

      // important: every page that calls useRequireRole(Role.PARTNER) is listed here too, so a
      // tenant deep link is sent home before the page shell mounts.
      // scripts/checks/check-role-gates.mjs keeps the lists in step.
      const adminOrPartnerPaths = [
        "/inventory/identities",
        "/inventory/groups",
        "/inventory/clustering/members",
        "/inventory/clusters",
        "/inventory/operations",
        "/inventory/server",
        "/inventory/snapshots",
        "/inventory/migrations",
        "/inventory/warnings",
        "/my-keys",
        // Installing an add-on hands a third party a credential into an organization, so the
        // gate is a real redirect here rather than a component that renders null.
        "/extensions",
        ...EDITION_ADMIN_OR_PARTNER_PATHS,
      ];

      const isAdminOrPartnerRoute = adminOrPartnerPaths.some(
        (p) => nextUrl.pathname === p || nextUrl.pathname.startsWith(p + "/")
      );

      // Host Health names internal addresses and unseals Vault: a platform operator's page.
      // The API refuses a non-admin on its own; this keeps the page shell from loading at all.
      const adminOnlyPaths = ["/inventory/warnings/host-health", ...EDITION_ADMIN_ONLY_PATHS];
      const isAdminOnlyRoute = adminOnlyPaths.some(
        (p) => nextUrl.pathname === p || nextUrl.pathname.startsWith(p + "/")
      );

      // A session with no role at all is left to the access gate in MainLayout, which shows
      // the no-role notice on any page and keeps the URL, so Check again lands where the person
      // was going. The partner redirect is for a role below Partner.
      const belowPartner = hasMappedRole(userGroups) && !isPartner;

      if ((isAdminOrPartnerRoute && belowPartner) || (isAdminOnlyRoute && !isAdmin)) {
        return Response.redirect(redirectTo(nextUrl, host, "/"));
      }

      return true;
    },
  },

  debug: process.env.NODE_ENV === "development",

  // Trust the Host header the reverse proxy forwards when building callback URLs.
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
// The eager form: the lazy form, NextAuth((request) => config), returns promises of the
// handlers, and nothing here needs the request to build the config.
export const { handlers } = NextAuth(authConfig);
