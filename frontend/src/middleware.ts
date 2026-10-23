import { auth } from "@/auth";
import { inspectRequestTarget } from "@/lib/request-security";
import { dropUnchangedSessionCookie } from "@/lib/session-cookie-echo";
import { NextResponse, type NextRequest } from "next/server";
import { isSettingsTab } from "@/app/settings/settings-tabs";

// note: middleware cannot tell a router prefetch from a real navigation. Next strips every
// FLIGHT_HEADER (RSC, Next-Router-Prefetch, Next-Router-State-Tree, ...) from the request
// before invoking middleware, deliberately: see server/web/adapter.js, "Headers should only
// be stripped for middleware". Verified on 15.5.18 by dumping the header names seen here.
// Anything that needs to treat a prefetch differently has to live outside this chain.
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

  // note: App Router may stream a not-found page with HTTP 200. Reject unknown Settings
  // paths before rendering so the URL contract also holds for clients checking status codes.
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
  return dropUnchangedSessionCookie(request, response, process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET);
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
