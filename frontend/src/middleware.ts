/**
 * Next.js Middleware for Authentication
 *
 * This middleware runs on every request and ensures:
 * - All protected routes require authentication
 * - Unauthenticated users are redirected to sign-in page
 * - Auth pages are accessible without authentication
 */

export { auth as middleware } from "@/auth";

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
