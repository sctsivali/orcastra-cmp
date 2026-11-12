import { getToken, type JWT } from "next-auth/jwt";

// The two names NextAuth gives the session cookie: `__Secure-` when its URL is https, plain
// otherwise. A large session is split into `<name>.0`, `<name>.1`, ...
export const SESSION_COOKIES = ["__Secure-authjs.session-token", "authjs.session-token"] as const;

export function sessionCookieBase(name: string): string | undefined {
  return SESSION_COOKIES.find((base) => name === base || name.startsWith(`${base}.`));
}

/** The session cookie name a Cookie header carries, if any. The secure name wins when both do. */
export function presentSessionCookie(cookieHeader: string | null | undefined): string | undefined {
  if (!cookieHeader) return undefined;
  const names = new Set(
    cookieHeader
      .split(";")
      .map((pair) => sessionCookieBase(pair.slice(0, pair.indexOf("=")).trim()))
      .filter((base): base is string => Boolean(base)),
  );
  return SESSION_COOKIES.find((base) => names.has(base));
}

/**
 * The session a request's cookies carry: undefined when they carry none, null when they carry one
 * this deployment cannot read (another secret, a truncated chunk, an empty value).
 */
export async function readSessionCookie(
  requestHeaders: Headers,
  secret: string | undefined,
): Promise<JWT | null | undefined> {
  const cookieHeader = requestHeaders.get("cookie");
  const base = presentSessionCookie(cookieHeader);
  if (!base) return undefined;
  if (!secret) return null;
  // important: the Cookie header alone, never requestHeaders. getToken falls back to an
  // Authorization bearer when the cookie is empty, and that bearer is whatever the caller sent.
  return getToken({
    req: { headers: new Headers({ cookie: cookieHeader ?? "" }) },
    secret,
    cookieName: base,
    secureCookie: base.startsWith("__Secure-"),
  });
}
