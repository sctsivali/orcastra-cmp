import { getToken, type JWT } from "next-auth/jwt";

// important: NextAuth's middleware re-encodes the session and appends its Set-Cookie to every
// response after our handler has run (next-auth/lib/index.js, "Preserve cookies from the session
// response"). When middleware did not refresh, that cookie is the request's own session again.
// A navigation that lands after /api/auth/session refreshed then puts the older session back:
// a refresh token the IdP has just rotated, or a cooldown the background path has just set
// (security review of the refresh cooldown split, 2026-10-23). So a session cookie only leaves
// middleware when the session in it changed.

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
