import { auth } from "@/auth";
import { inspectRequestTarget } from "@/lib/request-security";
import { NextResponse } from "next/server";

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

  return undefined;
});

export const config = {
  // Match all routes except static assets and API routes handled by NextAuth
  matcher: [
    /*
     * Match all request paths except for the ones starting with:
     * - api (API routes: NextAuth + backend proxy)
     * - health (health check proxy)
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon (favicon files)
     * - public assets (images, fonts, etc.)
     */
    "/((?!api/|health|_next/static|_next/image|favicon|.*\\.ico|.*\\.png|.*\\.jpg|.*\\.svg|.*\\.webp|.*\\.json|spice-html5).*)",
  ],
};
