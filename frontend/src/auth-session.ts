/**
 * The session a hard load can start from, read on the server. Server components only.
 *
 * Without it every document load rendered nothing until the browser had fetched
 * /api/auth/session (twice: next-auth's mount read echoes through its own BroadcastChannel), and
 * that route runs on the nodejs runtime with the 120 s background refresh skew (auth.ts), so a
 * reload inside the window also waited 1.2 to 2 s for the identity provider before any data was
 * requested.
 *
 * important: this is a second NextAuth instance whose jwt callback NEVER refreshes. The normal
 * `auth()` called from a server component may refresh an expired token, and a server component
 * cannot set cookies: next-auth's RSC path keeps only the JSON of its internal response. A
 * refresh here would spend the rotating refresh token and throw the new one away. Instead a
 * token that is not comfortably valid yields no session, and the browser falls back to fetching
 * one exactly as it did before.
 *
 * important: the cookie read is the one the browser sent. Middleware may have refreshed an
 * expired token on this same request, but next-auth sets that cookie on a plain Response, which
 * Next does not merge into this render's cookies. Such a token fails the validity margin, so
 * the fallback covers it.
 */
import NextAuth from "next-auth";
import type { Session } from "next-auth";
import { authConfig } from "@/auth";

// Covers server render to client mount plus one background refresh (10 s budget, auth.ts), so a
// request sent with the hydrated bearer cannot reach the backend expired before the refresh lands.
const HYDRATE_MIN_VALIDITY_S = 60;

const { auth: peekSession } = NextAuth({
  ...authConfig,
  callbacks: {
    ...authConfig.callbacks,
    jwt: async ({ token }) => {
      const nowSeconds = Math.floor(Date.now() / 1000);
      if (token.error || !token.accessToken || !token.expiresAt) return null;
      return token.expiresAt - nowSeconds >= HYDRATE_MIN_VALIDITY_S ? token : null;
    },
  },
});

/**
 * A session to hydrate SessionProvider with, or undefined to let the browser fetch one.
 *
 * important: never null. SessionProvider reads null as "known signed out": it starts not
 * loading and its poll never fetches, which wedges the tab. And an explicit projection, never
 * the raw result: in a server component next-auth builds `{ user: token, ...session }`, so a
 * session callback that ever stopped returning `user` would serialize the whole JWT, refresh
 * token included, into the HTML.
 */
export async function readHydratableSession(): Promise<Session | undefined> {
  try {
    const session = await peekSession();
    if (!session?.user || !session.accessToken || session.error) return undefined;
    const { id, name, email, image, groups, roles } = session.user;
    return {
      expires: session.expires,
      accessToken: session.accessToken,
      user: {
        id,
        name: name ?? null,
        email: email ?? null,
        image: image ?? null,
        groups: groups ?? [],
        roles: roles ?? [],
      },
    };
  } catch {
    return undefined;
  }
}
