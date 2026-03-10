/**
 * NextAuth.js v5 Configuration for Authentik OAuth2/OIDC
 *
 * This configuration provides enterprise-grade SSO authentication
 * using Authentik as the Identity Provider.
 */

import NextAuth from "next-auth";
import type { NextAuthConfig, User, Session } from "next-auth";
import type { JWT } from "next-auth/jwt";

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

// Timeout for outbound fetch calls to the IdP (ms)
const IDP_FETCH_TIMEOUT_MS = 10_000;

// Cached OIDC token endpoint to avoid re-fetching discovery on every refresh
let _cachedTokenEndpoint: string | null = null;

// Token refresh function for handling expired tokens
async function refreshAccessToken(token: JWT): Promise<JWT> {
  try {
    const issuer = process.env.AUTHENTIK_ISSUER;
    if (!issuer) throw new Error("AUTHENTIK_ISSUER not configured");

    // Resolve token endpoint (cache discovery to avoid extra round-trip)
    if (!_cachedTokenEndpoint) {
      const discoveryUrl = `${issuer}.well-known/openid-configuration`;
      const discoveryResponse = await fetch(discoveryUrl, {
        signal: AbortSignal.timeout(IDP_FETCH_TIMEOUT_MS),
      });
      const discovery = await discoveryResponse.json();
      _cachedTokenEndpoint = discovery.token_endpoint;
    }

    const response = await fetch(_cachedTokenEndpoint!, {
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
      signal: AbortSignal.timeout(IDP_FETCH_TIMEOUT_MS),
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
    };
  } catch (error) {
    // Compact log - avoid dumping the full stack on every request
    const reason = error instanceof Error ? error.message : String(error);
    console.warn(`[AUTH] Token refresh failed: ${reason}`);

    // Invalidate cached endpoint in case discovery changed
    _cachedTokenEndpoint = null;

    return {
      ...token,
      error: "RefreshAccessTokenError",
      // Set a cooldown: pretend the token is valid for N seconds so we
      // don't retry on every single incoming request while the IdP is down.
      expiresAt: Math.floor(Date.now() / 1000) + TOKEN_REFRESH_COOLDOWN_SECONDS,
    };
  }
}

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
    async jwt({ token, user, account }) {
      // Initial sign in
      if (account && user) {
        // Use EMAIL as the persistent user identifier
        // This is more reliable than Authentik sub which can change per session
        const persistentUserId = user.email || user.id;
        
        const newToken = {
          ...token,
          accessToken: account.access_token,
          refreshToken: account.refresh_token,
          expiresAt: account.expires_at,
          groups: (user as any).groups || [],
          roles: (user as any).roles || [],
          // CRITICAL: Use email as persistent user ID
          // Authentik sub changes per session, email is stable
          authentikId: persistentUserId,
        };
        return newToken;
      }

      // Return previous token if the access token has not expired yet
      if (token.expiresAt && Date.now() < token.expiresAt * 1000) {
        return token;
      }

      // If a previous refresh already failed and we're still in cooldown,
      // skip retrying to avoid flooding the IdP with requests.
      if (token.error === "RefreshAccessTokenError") {
        // expiresAt was set to now + cooldown; this branch only triggers
        // when the cooldown period has also elapsed, so proceed to retry.
      }

      // Access token has expired, try to refresh it
      return await refreshAccessToken(token);
    },

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
    authorized({ auth, request: { nextUrl } }) {
      const isLoggedIn = !!auth?.user;
      const isOnAuthPage = nextUrl.pathname.startsWith("/auth");
      const isApiRoute = nextUrl.pathname.startsWith("/api");
      const isPublicAsset =
        nextUrl.pathname.startsWith("/_next") ||
        nextUrl.pathname.startsWith("/favicon") ||
        nextUrl.pathname.includes(".");

      // Allow public assets and API routes
      if (isPublicAsset || isApiRoute) {
        return true;
      }

      // Redirect logged-in users away from auth pages
      if (isOnAuthPage) {
        if (isLoggedIn) {
          return Response.redirect(new URL("/", nextUrl));
        }
        return true; // Allow access to auth pages for non-logged-in users
      }

      // Require authentication for all other pages
      if (!isLoggedIn) {
        return false;
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
      ];

      const isAdminOrPartnerRoute = adminOrPartnerPaths.some(
        (p) => nextUrl.pathname === p || nextUrl.pathname.startsWith(p + "/")
      );

      if (isAdminOrPartnerRoute && !isPartner) {
        return Response.redirect(new URL("/", nextUrl));
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

// Export NextAuth handlers and helpers
export const { handlers, auth, signIn, signOut } = NextAuth(authConfig);
