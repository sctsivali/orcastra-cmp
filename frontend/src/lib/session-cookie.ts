import { getToken, type JWT } from "next-auth/jwt";

// The names the session cookie can carry: `__Host-` on a deployment whose configured URL is https
// (pinnedSessionCookies below), NextAuth's own `__Secure-` when only the request is https, plain
// otherwise. A large session is split into `<name>.0`, `<name>.1`, ...
export const HOST_SESSION_COOKIE = "__Host-authjs.session-token";
export const SESSION_COOKIES = [HOST_SESSION_COOKIE, "__Secure-authjs.session-token", "authjs.session-token"] as const;

function configuredProtocol(configuredUrl: string | undefined): string | undefined {
  try {
    return configuredUrl ? new URL(configuredUrl).protocol : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The session cookie an https deployment pins, for NextAuth's `cookies` option, or undefined to
 * keep NextAuth's own naming.
 *
 * important: `__Host-` makes the browser refuse the cookie unless it is Secure, Path=/ and has no
 * Domain, so no other host on the same site (a sibling subdomain, another service on the parent
 * domain) can set a session cookie this deployment would read, or shadow it with a narrower path.
 * Only for a configured https URL: a browser drops a Secure cookie set over plain http.
 */
export function pinnedSessionCookies(configuredUrl: string | undefined) {
  if (configuredProtocol(configuredUrl) !== "https:") return undefined;
  return {
    sessionToken: {
      name: HOST_SESSION_COOKIE,
      options: { httpOnly: true, sameSite: "lax" as const, path: "/", secure: true },
    },
  };
}

export function sessionCookieBase(name: string): string | undefined {
  return SESSION_COOKIES.find((base) => name === base || name.startsWith(`${base}.`));
}

/**
 * The name the session cookie has for this request: `__Host-` when the configured AUTH_URL or
 * NEXTAUTH_URL is https (pinnedSessionCookies), plain when it is http, and NextAuth's own choice
 * (@auth/core init) when none is configured: `__Secure-` for a request that came in over https.
 *
 * important: never "whichever name is present". A host on the same site can plant the other name
 * with a narrower Path (Path=/api/v1), which the browser sends first, and swap or blank the session
 * the proxy reads while NextAuth keeps reading the real one.
 */
export function sessionCookieName(requestHeaders: Headers, requestUrl: string, configuredUrl: string | undefined): string {
  const pinned = pinnedSessionCookies(configuredUrl);
  if (pinned) return pinned.sessionToken.name;
  let protocol = configuredProtocol(configuredUrl);
  if (!protocol) {
    const forwarded = requestHeaders.get("x-forwarded-proto")?.split(",")[0].trim();
    protocol = forwarded ? `${forwarded.replace(/:$/, "")}:` : new URL(requestUrl).protocol;
  }
  return protocol === "https:" ? "__Secure-authjs.session-token" : "authjs.session-token";
}

/** Whether a Cookie header carries the named session cookie, whole or chunked. */
export function carriesSessionCookie(cookieHeader: string | null | undefined, name: string): boolean {
  if (!cookieHeader) return false;
  return cookieHeader.split(";").some((pair) => {
    const cookie = pair.slice(0, pair.indexOf("=")).trim();
    return cookie === name || cookie.startsWith(`${name}.`);
  });
}

/**
 * The session a request's cookies carry under `name`: undefined when they carry none, null when
 * they carry one this deployment cannot read (another secret, a truncated chunk, an empty value).
 */
export async function readSessionCookie(
  requestHeaders: Headers,
  name: string,
  secret: string | undefined,
): Promise<JWT | null | undefined> {
  const cookieHeader = requestHeaders.get("cookie");
  if (!carriesSessionCookie(cookieHeader, name)) return undefined;
  if (!secret) return null;
  // important: the Cookie header alone, never requestHeaders. getToken falls back to an
  // Authorization bearer when the cookie is empty, and that bearer is whatever the caller sent.
  return getToken({
    req: { headers: new Headers({ cookie: cookieHeader ?? "" }) },
    secret,
    cookieName: name,
    secureCookie: name.startsWith("__Secure-") || name.startsWith("__Host-"),
  });
}
