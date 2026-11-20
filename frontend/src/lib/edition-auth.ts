/**
 * How an edition other than Full signs in. Full signs in through the identity provider in
 * auth.ts and sets nothing here.
 *
 * important: auth.ts is also bundled into middleware, so an implementation must be safe to import
 * on the edge runtime. Server-only work (request headers, the backend) belongs inside its
 * functions, loaded with a dynamic import, never at module level.
 */

import type { NextAuthConfig } from "next-auth";
import type { JWT } from "next-auth/jwt";

export interface EditionAuth {
  providers: NextAuthConfig["providers"];
  /** The session token at sign-in, built from what the provider's authorize() returned. */
  signedIn(token: JWT, user: unknown, account: unknown): JWT | Promise<JWT>;
  /**
   * A renewed token for a session near expiry, the same token with `error` set when renewal
   * failed for a reason that may pass, or REFRESH_TOKEN_REVOKED when the session is over.
   */
  refresh(token: JWT): Promise<JWT | null>;
  /**
   * Adjusts the headers the /api/v1 proxy sends the backend. It runs after the proxy dropped the
   * hop-by-hop and client-address headers and before the session bearer is attached. Every other
   * header the browser sent is still present, so an edition that trusts a header from its edge
   * must delete the client's copy before setting its own. Runs on the nodejs runtime only.
   */
  forwardHeaders?(request: Request, headers: Headers): void;
}

export const editionAuth: EditionAuth | null = null;
