import { getToken, type JWT } from "next-auth/jwt";

// important: NextAuth re-encodes the session and sets it on every response that read it: every
// middleware response (appended after the handler runs, next-auth/lib/index.js, "Preserve
// cookies from the session response") and every /api/auth/session answer. A response that lands
// after another one rotated the session puts the older session back, with a refresh token the
// IdP has already revoked (Authentik rotates on every grant by default) or without a cooldown the
// background path has just set. So a session cookie is only sent when the session in it changed.

const SESSION_COOKIES = ["__Secure-authjs.session-token", "authjs.session-token"];
// Stamped by every encode, so they differ even when nothing in the session did.
const ENCODE_CLAIMS = new Set(["iat", "exp", "jti"]);

function cookieName(setCookie: string): string {
  return setCookie.slice(0, setCookie.indexOf("=")).trim();
}

function sessionCookieBase(name: string): string | undefined {
  return SESSION_COOKIES.find((base) => name === base || name.startsWith(`${base}.`));
}

function isDeletion(setCookie: string): boolean {
  const value = setCookie.slice(setCookie.indexOf("=") + 1).split(";")[0].trim();
  return value === "" || /;\s*max-age=0\s*(;|$)/i.test(setCookie);
}

export function sameSession(a: JWT, b: JWT): boolean {
  for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (ENCODE_CLAIMS.has(key)) continue;
    if (JSON.stringify(a[key]) !== JSON.stringify(b[key])) return false;
  }
  return true;
}

export async function dropUnchangedSessionCookie(
  request: Request,
  response: Response,
  secret: string | undefined,
): Promise<Response> {
  const setCookies = response.headers.getSetCookie();
  const session = setCookies.filter((cookie) => sessionCookieBase(cookieName(cookie)));
  // A deletion always goes through: sign-out, an unreadable session, a chunk being retired.
  if (!secret || session.length === 0 || session.some(isDeletion)) return response;

  const base = sessionCookieBase(cookieName(session[0]))!;
  // Cookie header only: getToken falls back to an Authorization bearer, which is not the session.
  const read = (cookie: string) =>
    getToken({
      req: { headers: new Headers({ cookie }) },
      secret,
      cookieName: base,
      secureCookie: base.startsWith("__Secure-"),
    });
  const [before, after] = await Promise.all([
    read(request.headers.get("cookie") ?? ""),
    read(session.map((cookie) => cookie.split(";")[0]).join("; ")),
  ]);
  if (!before || !after || !sameSession(before, after)) return response;

  const headers = new Headers(response.headers);
  headers.delete("set-cookie");
  for (const cookie of setCookies) {
    if (!sessionCookieBase(cookieName(cookie))) headers.append("set-cookie", cookie);
  }
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

/** NextAuth's own secret lookup (next-auth/lib/env.js). */
export function sessionSecret(): string | undefined {
  return process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET;
}

/** A route handler whose responses carry the session cookie only when the session changed. */
export function withoutSessionEcho<R extends Request>(
  handler: (request: R) => Promise<Response>,
): (request: R) => Promise<Response> {
  return async (request) => dropUnchangedSessionCookie(request, await handler(request), sessionSecret());
}
