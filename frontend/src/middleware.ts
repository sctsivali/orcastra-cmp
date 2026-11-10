import NextAuth from "next-auth";
import { authConfig } from "@/auth";
import { inspectRequestTarget } from "@/lib/request-security";
import { dropUnchangedSessionCookie, sessionSecret } from "@/lib/session-cookie-echo";
import { NextResponse, type NextRequest } from "next/server";
import { isSettingsTab } from "@/app/settings/settings-tabs";

// important: middleware reads the session and never refreshes it. /api/auth/session is the only
// writer (auth.ts): with an IdP that rotates refresh tokens, a second writer spends the same token
// and is refused. An expired access token here only means the page renders and the client's
// session read refreshes it; the role gate below reads the cookie's groups until then.
const { auth } = NextAuth({ ...authConfig, callbacks: { ...authConfig.callbacks, jwt: ({ token }) => token } });

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

  return undefined;
});

// NextAuth appends the session cookie after the handler above returns, so it can only be held
// back out here (see lib/session-cookie-echo.ts).
export default async function middleware(request: NextRequest, context: Parameters<typeof authMiddleware>[1]) {
  const response = await authMiddleware(request, context);
  if (!response) return response;
  return dropUnchangedSessionCookie(request, response, sessionSecret());
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
