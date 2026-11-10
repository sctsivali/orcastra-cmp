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
import { authConfig } from "@/auth";
import { maskToken, type HydratedSession } from "@/lib/hydrated-session";

// Covers server render to client mount plus one background refresh (up to 20 s, auth.ts), so a
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
 * loading and its poll never fetches, which wedges the tab. Always an explicit projection: in a
 * server component next-auth builds `{ user: token, ...session }`, so a session callback that
 * stopped returning `user` would serialize the whole JWT, refresh token included, into the HTML.
 * The access token itself travels masked (lib/hydrated-session.ts).
 */
export async function readHydratableSession(): Promise<HydratedSession | undefined> {
  try {
    const session = await peekSession();
    if (!session?.user || !session.accessToken || session.error) return undefined;
    const { id, name, email, image, groups, roles } = session.user;
    return {
      expires: session.expires,
      maskedAccessToken: maskToken(session.accessToken),
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
