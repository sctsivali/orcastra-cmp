import { auth } from "@/auth";
import { inspectRequestTarget } from "@/lib/request-security";
import { NextResponse } from "next/server";
import { isSettingsTab } from "@/app/settings/settings-tabs";

// note: middleware cannot tell a router prefetch from a real navigation. Next strips every
// FLIGHT_HEADER (RSC, Next-Router-Prefetch, Next-Router-State-Tree, ...) from the request
// before invoking middleware, deliberately: see server/web/adapter.js, "Headers should only
// be stripped for middleware". Verified on 15.5.18 by dumping the header names seen here.
// Anything that needs to treat a prefetch differently has to live outside this chain.
export default auth((request) => {
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

export const config = {
  // Match all routes except static assets and API routes handled by NextAuth
  matcher: [
    /*
     * Match all request paths except for the ones starting with:
     * - api (API routes: NextAuth + backend proxy)
     * - health (readiness proxy) and healthz (the Docker liveness probe): both must stay
     *   reachable without a session, so the prefix stays here
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon (favicon files)
     * - public assets (images, fonts, etc.)
     */
    "/((?!api/|health|_next/static|_next/image|favicon|.*\\.ico|.*\\.png|.*\\.jpg|.*\\.svg|.*\\.webp|.*\\.json|spice-html5).*)",
  ],
};
