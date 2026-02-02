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
    // DEBUG: Log Authentik profile to see what sub is being sent
    console.log("[AUTH DEBUG] Authentik profile received:", JSON.stringify({
      sub: profile.sub,
      email: profile.email,
      name: profile.name,
      preferred_username: profile.preferred_username,
    }, null, 2));
    
    return {
      // Use email as primary ID since Authentik sub can change per session
      id: profile.email,
      name: profile.name || profile.preferred_username,
      email: profile.email,
      image: profile.picture,
      groups: profile.groups || [],
      roles: profile.groups?.filter((g: string) => g.startsWith("role_")) || [],
      // Store original Authentik sub for reference
      authentikSub: profile.sub,
    };
  },
};

// Token refresh function for handling expired tokens
async function refreshAccessToken(token: JWT): Promise<JWT> {
  try {
    const issuer = process.env.AUTHENTIK_ISSUER;
    if (!issuer) throw new Error("AUTHENTIK_ISSUER not configured");

    // Fetch OIDC discovery document to get token endpoint
    const discoveryUrl = `${issuer}.well-known/openid-configuration`;
    const discoveryResponse = await fetch(discoveryUrl);
    const discovery = await discoveryResponse.json();

    const response = await fetch(discovery.token_endpoint, {
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
    console.error("Error refreshing access token:", error);
    return {
      ...token,
      error: "RefreshAccessTokenError",
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
        // DEBUG: Log what's being stored in token
        console.log("[AUTH DEBUG] JWT callback - Initial sign in:", JSON.stringify({
          "user.id": user.id,
          "user.email": user.email,
          "token.sub (before)": token.sub,
        }, null, 2));
        
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
        
        console.log("[AUTH DEBUG] JWT callback - Token created with authentikId (email):", newToken.authentikId);
        return newToken;
      }

      // Return previous token if the access token has not expired yet
      if (token.expiresAt && Date.now() < token.expiresAt * 1000) {
        return token;
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
      return isLoggedIn;
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
