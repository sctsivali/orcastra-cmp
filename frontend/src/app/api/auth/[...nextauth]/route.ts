/** Auth.js handlers for /api/auth/*: sign-in, OAuth callbacks, sign-out and the session read. */

import { handlers } from "@/auth";
import { withoutSessionEcho } from "@/lib/session-cookie-echo";

// The session is written back only when it changed (lib/session-cookie-echo.ts). Sign-out writes
// deletions and sign-in a new session, so both still go out.
export const GET = withoutSessionEcho(handlers.GET);
export const POST = withoutSessionEcho(handlers.POST);
