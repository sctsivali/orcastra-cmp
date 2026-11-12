/**
 * Who the /api/v1 proxy speaks for.
 *
 * A browser request carries the session cookie, and the proxy attaches that session's access token
 * itself, so the token never has to reach page JavaScript. Because the cookie travels on its own,
 * the proxy has to refuse what a cookie-authenticated endpoint must refuse: a write another site
 * started (cross-site request forgery), a session the identity provider ended, and a tab still
 * acting for an account that is no longer the one signed in. A request without a session cookie
 * (an integration's API key, an MCP client, a script) passes through with its own Authorization.
 */
import type { JWT } from "next-auth/jwt";
import { REFRESH_TOKEN_REVOKED } from "@/lib/auth-refresh";
import { readSessionCookie } from "@/lib/session-cookie";

/** The signed-in account the page was rendered for, sent URI-encoded by the API client. */
export const ACTOR_HEADER = "x-orcastra-actor";
/** Names why the proxy answered 401 itself, for the API client's session handling. */
export const AUTH_SIGNAL_HEADER = "x-orcastra-auth";

export type ProxyAuthRefusal = {
  kind: "refuse";
  status: 401 | 403;
  signal: "session-ended" | "actor-changed" | "cross-site";
  detail: string;
};

export type ProxyAuth =
  | { kind: "session"; bearer: string }
  // Forwarded without the session: the caller's own Authorization, if any, decides.
  | { kind: "anonymous" }
  | ProxyAuthRefusal;

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** The account a session belongs to, as the session's `user.id` names it (auth.ts). */
export function sessionSubject(token: JWT): string {
  return token.authentikId || token.sub || "";
}

function originOf(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    return new URL(value).origin;
  } catch {
    return undefined;
  }
}

/**
 * Whether an Origin header names this deployment: the configured NEXTAUTH_URL, or the host the
 * request was addressed to (a second hostname such as a tunnel). A forged Host does not help a
 * cross-site page, which cannot choose the headers of a request the browser sends for it.
 */
function originIsThisDeployment(origin: string, headers: Headers, configuredUrl: string | undefined): boolean {
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  if (parsed.origin === "null") return false;
  if (originOf(configuredUrl) === parsed.origin) return true;
  const addressed = (headers.get("x-forwarded-host") ?? headers.get("host") ?? "").split(",")[0].trim().toLowerCase();
  return addressed !== "" && parsed.host.toLowerCase() === addressed;
}

/**
 * Whether the browser (or a non-browser client) sent this request from this deployment's own
 * pages. Browsers attach Sec-Fetch-Site on https and Origin on every write; a script or the QA
 * harness with the cookie sends neither.
 */
function fromThisDeployment(method: string, headers: Headers, configuredUrl: string | undefined): boolean {
  const site = headers.get("sec-fetch-site");
  const origin = headers.get("origin");
  // The browser's own verdict, a header no page can set, wins over matching Origin to Host.
  if (site === "cross-site" || site === "same-site") return false;
  if (site === "same-origin") return true;
  if (origin !== null) return originIsThisDeployment(origin, headers, configuredUrl);
  // important: a typed address or a bookmark (`none`) may read, never write; a write without
  // Origin from a browser does not happen.
  if (site === "none") return SAFE_METHODS.has(method);
  return true;
}

function decodedActor(value: string): string | undefined {
  try {
    return decodeURIComponent(value);
  } catch {
    return undefined;
  }
}

/** The decision for one request. `session` is undefined without a cookie, null when unreadable. */
export function decideProxyAuth(
  method: string,
  headers: Headers,
  session: JWT | null | undefined,
  configuredUrl: string | undefined,
): ProxyAuth {
  if (!session) return { kind: "anonymous" };
  const verb = method.toUpperCase();
  if (!fromThisDeployment(verb, headers, configuredUrl)) {
    if (SAFE_METHODS.has(verb)) return { kind: "anonymous" };
    return {
      kind: "refuse", status: 403, signal: "cross-site",
      detail: "This request did not come from this dashboard's own pages.",
    };
  }
  if (session.error === REFRESH_TOKEN_REVOKED || !session.accessToken) {
    return { kind: "refuse", status: 401, signal: "session-ended", detail: "The session has ended. Sign in again." };
  }
  const actor = headers.get(ACTOR_HEADER);
  if (actor !== null && decodedActor(actor) !== sessionSubject(session)) {
    return {
      kind: "refuse", status: 401, signal: "actor-changed",
      detail: "Another account signed in on this browser. Reload the page to continue as that account.",
    };
  }
  return { kind: "session", bearer: session.accessToken };
}

export async function resolveProxyAuth(
  request: Request,
  secret: string | undefined,
  configuredUrl: string | undefined,
): Promise<ProxyAuth> {
  const session = await readSessionCookie(request.headers, secret);
  return decideProxyAuth(request.method, request.headers, session, configuredUrl);
}

/**
 * Rewrites the response headers for the browser leg. An answer is same-origin, so upstream CORS
 * headers have no reader. An answer made with the session depends on the cookie, which a shared
 * cache keys on only when told: unlike an Authorization header, a cookie does not make a response
 * private by itself (RFC 9111).
 */
export function finishProxyResponse(headers: Headers, auth: ProxyAuth): void {
  for (const name of [...headers.keys()]) {
    if (name.startsWith("access-control-")) headers.delete(name);
  }
  if (auth.kind !== "session") return;
  const vary = headers.get("vary");
  if (!vary) headers.set("vary", "Cookie");
  else if (!/(^|,)\s*(cookie|\*)\s*(,|$)/i.test(vary)) headers.set("vary", `${vary}, Cookie`);
  const cacheControl = headers.get("cache-control");
  if (!cacheControl) headers.set("cache-control", "private");
  else if (/(^|,)\s*public\s*(,|$)/i.test(cacheControl)) {
    headers.set("cache-control", cacheControl.replace(/(^|,)(\s*)public(\s*)(?=,|$)/i, "$1$2private$3"));
  } else if (!/(^|,)\s*(private|no-store)\s*(,|$)/i.test(cacheControl)) {
    headers.set("cache-control", `private, ${cacheControl}`);
  }
}

/** Rewrites the upstream headers: the session's bearer or the caller's own, never the cookie. */
export function applyProxyAuth(headers: Headers, auth: ProxyAuth): void {
  // The backend authenticates by bearer alone. The cookie holds the refresh token, the browser's
  // Origin would make the backend reflect CORS onto a same-origin answer, and the actor hint has
  // already been checked here.
  headers.delete("cookie");
  headers.delete("origin");
  headers.delete(ACTOR_HEADER);
  if (auth.kind === "session") headers.set("authorization", `Bearer ${auth.bearer}`);
}
