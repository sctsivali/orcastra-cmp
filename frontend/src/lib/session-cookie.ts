import { getToken, type JWT } from "next-auth/jwt";

// The two names NextAuth gives the session cookie: `__Secure-` when its URL is https, plain
// otherwise. A large session is split into `<name>.0`, `<name>.1`, ...
export const SESSION_COOKIES = ["__Secure-authjs.session-token", "authjs.session-token"] as const;

export function sessionCookieBase(name: string): string | undefined {
  return SESSION_COOKIES.find((base) => name === base || name.startsWith(`${base}.`));
}

/**
 * The name NextAuth itself gives the session cookie for this request (@auth/core init): secure when
 * the configured AUTH_URL or NEXTAUTH_URL is https, else when the request came in over https.
 *
 * important: never "whichever name is present". A host on the same site can plant the other name
 * with a narrower Path (Path=/api/v1), which the browser sends first, and swap or blank the session
 * the proxy reads while NextAuth keeps reading the real one.
 */
export function sessionCookieName(requestHeaders: Headers, requestUrl: string, configuredUrl: string | undefined): string {
  let protocol: string | undefined;
  try {
    if (configuredUrl) protocol = new URL(configuredUrl).protocol;
  } catch {
    protocol = undefined;
  }
  if (!protocol) {
    const forwarded = requestHeaders.get("x-forwarded-proto")?.split(",")[0].trim();
    protocol = forwarded ? `${forwarded.replace(/:$/, "")}:` : new URL(requestUrl).protocol;
  }
  return protocol === "https:" ? SESSION_COOKIES[0] : SESSION_COOKIES[1];
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
    secureCookie: name.startsWith("__Secure-"),
  });
}
