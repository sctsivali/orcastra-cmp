/**
 * NextAuth.js API Route Handler
 *
 * This file exports the HTTP handlers for NextAuth.js.
 * It handles:
 * - GET /api/auth/signin - Sign in page
 * - POST /api/auth/signin - Sign in action
 * - GET /api/auth/callback/* - OAuth callbacks
 * - POST /api/auth/signout - Sign out action
 * - GET /api/auth/session - Get session
 */

import { handlers } from "@/auth";
import { withoutSessionEcho } from "@/lib/session-cookie-echo";

// The session is written back only when it changed (lib/session-cookie-echo.ts). Sign-out writes
// deletions and sign-in a new session, so both still go out.
export const GET = withoutSessionEcho(handlers.GET);
export const POST = withoutSessionEcho(handlers.POST);
