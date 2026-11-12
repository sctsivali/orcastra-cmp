/**
 * The session a hard load can start from, read on the server. Server components only.
 *
 * It lets the first render send data requests without waiting on /api/auth/session, which runs
 * on the nodejs runtime with the 120 s background refresh skew (auth.ts) and can wait on the
 * identity provider.
 *
 * important: this is a second NextAuth instance whose jwt callback never refreshes. A server
 * component cannot set cookies (next-auth's RSC path keeps only the JSON of its internal
 * response), so a refresh here would spend the rotating refresh token and lose the new one. A
 * token that is not comfortably valid, including the expired one the browser sent (middleware
 * never refreshes), yields no session, and the browser's own session read refreshes it.
 */
import NextAuth from "next-auth";
import type { Session } from "next-auth";
import { authConfig } from "@/auth";

// Covers server render to client mount plus one background refresh (up to 20 s, auth.ts), so a
// request the hydrated page sends cannot reach the backend with a token that expired meanwhile.
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
 * loading and its poll never fetches, which wedges the tab. Always an explicit projection: in a
 * server component next-auth builds `{ user: token, ...session }`, so a session callback that
 * stopped returning `user` would serialize the whole JWT, refresh token included, into the HTML.
 * It carries the token's stamp, never the token (auth.ts session callback).
 */
export async function readHydratableSession(): Promise<Session | undefined> {
  try {
    const session = await peekSession();
    if (!session?.user || !session.tokenStamp || session.error) return undefined;
    const { id, name, email, image, groups, roles } = session.user;
    return {
      expires: session.expires,
      tokenStamp: session.tokenStamp,
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
