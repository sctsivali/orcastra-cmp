/**
 * Where an auth redirect points when the `authorized` callback refuses a page.
 *
 * important: next-auth's `handleAuth` parses the `Location` of any Response the callback
 * returns with `new URL(location)` (node_modules/next-auth/lib/index.js, isSameAuthAction),
 * so the value must stay absolute; a relative path throws and 500s every signed-out page load.
 *
 * The origin next-auth hands the callback is not the one the request arrived on: with
 * NEXTAUTH_URL set, `reqWithEnvURL` rewrites it to the public origin. A liveness probe that
 * asks `http://127.0.0.1:2025/` therefore used to be sent to `https://<public host>/auth/signin`,
 * and a probe that follows redirects then judged the container by the health of the edge in
 * front of it. The `Host` header survives the rewrite, so a request that arrived over loopback
 * is answered with a loopback redirect and never leaves the box. Only loopback hosts are
 * reflected, so this is not an open redirect.
 */

const LOOPBACK_NAMES = new Set(["localhost", "[::1]", "::1"]);

function bareHost(hostHeader: string): string {
  const host = hostHeader.trim().toLowerCase();
  if (host.startsWith("[")) {
    const end = host.indexOf("]");
    return end === -1 ? host : host.slice(0, end + 1);
  }
  return host.replace(/:\d+$/, "");
}

export function isLoopbackHost(hostHeader: string | null | undefined): boolean {
  if (!hostHeader) return false;
  const bare = bareHost(hostHeader);
  return LOOPBACK_NAMES.has(bare) || /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(bare);
}

/** The origin a redirect should carry: the request's own loopback host, else the app origin. */
export function redirectOrigin(nextUrl: URL, hostHeader: string | null | undefined): string {
  if (isLoopbackHost(hostHeader)) return `http://${hostHeader!.trim()}`;
  return nextUrl.origin;
}

/** An absolute redirect URL for `pathname` on the origin the request should stay on. */
export function redirectTo(
  nextUrl: URL,
  hostHeader: string | null | undefined,
  pathname: string,
  params?: Record<string, string>
): URL {
  const target = new URL(pathname, redirectOrigin(nextUrl, hostHeader));
  for (const [key, value] of Object.entries(params ?? {})) {
    target.searchParams.set(key, value);
  }
  return target;
}

/** The page to return to after sign-in, relative so it resolves on whichever origin signs in. */
export function callbackPath(nextUrl: URL): string {
  return `${nextUrl.pathname}${nextUrl.search}`;
}
