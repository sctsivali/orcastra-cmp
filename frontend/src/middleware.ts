import NextAuth from "next-auth";
import { authConfig } from "@/auth";
import { inspectRequestTarget } from "@/lib/request-security";
import { contentSecurityPolicies, newNonce, scriptPolicyMode, tileImageSources } from "@/lib/csp";
import { dropUnchangedSessionCookie, sessionSecret } from "@/lib/session-cookie-echo";
import { legacySessionCookieExpiries } from "@/lib/session-cookie";
import { NextResponse, type NextRequest } from "next/server";
import { isSettingsTab } from "@/app/settings/settings-tabs";

// important: middleware reads the session and never refreshes it. /api/auth/session is the only
// writer (auth.ts): with an IdP that rotates refresh tokens, a second writer spends the same token
// and is refused. An expired access token here only means the page renders and the client's
// session read refreshes it; the role gate below reads the cookie's groups until then.
const { auth } = NextAuth({ ...authConfig, callbacks: { ...authConfig.callbacks, jwt: ({ token }) => token } });

/**
 * A page response with this request's Content Security Policy (lib/csp.ts). The nonce reaches the
 * render twice: in x-nonce for the root layout's own scripts, and in the request's
 * Content-Security-Policy header, from which Next nonces its bootstrap and chunk scripts.
 *
 * important: set on the response here, inside the auth callback. NextAuth then adds the session
 * cookie to this same response and the echo guard below copies every header but that cookie, so
 * the policy survives both.
 *
 * important: Next copies a middleware response's headers onto the request it renders
 * (router-utils/resolve-routes) and reads the nonce from that request's Content-Security-Policy,
 * before its -Report-Only. So the response's enforced policy is what Next reads. In report-only
 * mode that policy has no script-src, and setting it would strip the nonce from every script Next
 * renders, which then reports its own pages; the static baseline in next.config.js sends the same
 * directives instead, and the request keeps the policy that carries the nonce.
 */
function withContentSecurityPolicy(request: NextRequest): NextResponse {
  const nonce = newNonce();
  const mode = scriptPolicyMode(process.env.CSP_SCRIPT_POLICY);
  const { enforced, reportOnly } = contentSecurityPolicies({
    nonce,
    dev: process.env.NODE_ENV === "development",
    mode,
    imageSources: tileImageSources(process.env),
  });
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", mode === "enforce" ? enforced : reportOnly);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  if (mode === "enforce") response.headers.set("Content-Security-Policy", enforced);
  response.headers.set("Content-Security-Policy-Report-Only", reportOnly);
  return response;
}

// Middleware cannot tell a router prefetch from a real navigation. Next strips every
// FLIGHT_HEADER (RSC, Next-Router-Prefetch, Next-Router-State-Tree, ...) from the request
// before invoking middleware (server/web/adapter.js, "Headers should only be stripped for
// middleware"; confirmed on Next 15.5.18). Prefetch-specific handling has to live elsewhere.
const authMiddleware = auth((request) => {
  const decision = inspectRequestTarget(
    request.method,
    request.nextUrl.pathname,
    request.nextUrl.search
  );

  if (decision) {
    return new NextResponse(decision.statusCode === 404 ? "Not found" : "Method not allowed", {
      status: decision.statusCode,
      headers: {
        "X-Orcastra-Security-Action": decision.reason,
      },
    });
  }

  // App Router may stream a not-found page with HTTP 200. Unknown Settings paths are rejected
  // before rendering so clients that check status codes see the 404.
  if (request.nextUrl.pathname.startsWith("/settings/") &&
      !isSettingsTab(request.nextUrl.pathname.slice("/settings/".length))) {
    return new NextResponse("Not found", { status: 404 });
  }

  return withContentSecurityPolicy(request);
});

// NextAuth appends the session cookie after the handler above returns, so it can only be held
// back out here (see lib/session-cookie-echo.ts).
export default async function middleware(request: NextRequest, context: Parameters<typeof authMiddleware>[1]) {
  const response = await authMiddleware(request, context);
  if (!response) return response;
  const final = await dropUnchangedSessionCookie(request, response, sessionSecret());
  // After the echo guard, which lets any session deletion through with every other session cookie:
  // expiring the pre-__Host- cookie must not carry an older session along with it.
  for (const expiry of legacySessionCookieExpiries(request.headers.get("cookie"), process.env.AUTH_URL ?? process.env.NEXTAUTH_URL)) {
    final.headers.append("set-cookie", expiry);
  }
  return final;
}

export const config = {
  // Match all pages; exclude only explicit public endpoints and asset locations.
  matcher: [
    /*
     * Dotted dynamic page names remain inside auth. Public files live at the root,
     * under geo/*.json, or under spice-html5/. Health probes are exact segments.
     */
    "/((?!api(?:/|$)|health(?:z)?(?:/|$)|_next/static(?:/|$)|_next/image(?:/|$)|[^/]+\\.(?:ico|png|jpg|svg|webp|json)$|geo/[^/]+\\.json$|spice-html5/).*)",
  ],
};
