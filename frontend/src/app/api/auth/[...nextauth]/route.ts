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

export const { GET, POST } = handlers;
