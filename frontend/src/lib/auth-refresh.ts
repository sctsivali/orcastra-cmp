/**
 * The identity provider's token endpoint, and what a failed refresh means.
 *
 * important: a refused refresh is not always worth retrying. Authentik (2026.2) deletes a refresh
 * token with the session it belongs to and rotates it on every grant, so `invalid_grant` means the
 * token is gone for good: the session ended at the IdP, or another copy already spent it. Treating
 * that as transient kept the expired access token in the cookie and retried every minute, so the
 * page looked signed in while every request and every socket ticket came back 401. Only that
 * answer ends the session; a timeout, a network failure, a 5xx or a 429 says nothing about the
 * token, and neither does a client misconfiguration (logged at error so it is seen).
 *
 * Pure: fetch, the clock and the logger are passed in, so scripts/checks drive it directly.
 * note: a log line never carries token material; only the status, the OAuth error code and timing.
 */

export type RefreshFailureKind = "revoked" | "transient";

/**
 * The session's `error` once the IdP has refused its refresh token: the session is over, and
 * middleware (auth.ts authorized) and the page (auth-provider.tsx) sign in again. A transient
 * failure stays REFRESH_FAILED with its cooldown.
 */
export const REFRESH_TOKEN_REVOKED = "RefreshTokenRevoked";
export const REFRESH_FAILED = "RefreshAccessTokenError";

export interface GrantedTokens {
  accessToken: string;
  /** The rotated refresh token, or the one presented when the IdP does not rotate. */
  refreshToken: string;
  /** Seconds the access token lives, as the IdP said. */
  expiresIn: number;
}

export type GrantResult =
  | { ok: true; tokens: GrantedTokens }
  | { ok: false; kind: RefreshFailureKind };

export interface IdpClientOptions {
  issuer: string | undefined;
  clientId: string;
  clientSecret: string;
  /** One deadline shared by discovery and the grant. */
  timeoutMs: number;
  fetch: typeof fetch;
  /** Milliseconds, like Date.now. */
  now: () => number;
  log: (level: "info" | "warn" | "error", line: string) => void;
  /** Which runtime asked, for the log line ("nodejs" in the route handlers). */
  runtime: string;
}

interface Discovery {
  token_endpoint: string;
  revocation_endpoint?: string;
}

// OAuth error codes are short snake_case words; anything else is dropped from the log.
const ERROR_CODE = /^[a-z_]{1,40}$/;

/** What a failed grant means, from its HTTP status and OAuth `error`. */
export function classifyRefreshFailure(
  status: number | undefined,
  error: string | undefined,
): { kind: RefreshFailureKind; level: "warn" | "error" } {
  if ((status === 400 || status === 401) && error === "invalid_grant") return { kind: "revoked", level: "warn" };
  // A client the IdP does not recognise is this deployment's fault, not the user's session.
  if (error === "invalid_client" || error === "unauthorized_client") return { kind: "transient", level: "error" };
  return { kind: "transient", level: "warn" };
}

class GrantFailure extends Error {
  constructor(readonly status: number | undefined, readonly code: string | undefined) {
    super(code ?? `HTTP ${status}`);
  }
}

export function createIdpClient(options: IdpClientOptions) {
  // note: called as plain fetch on purpose, so check-mutation-coherence audits the two POSTs below
  // against its raw-fetch allowlist like any other.
  const { fetch } = options;
  let cachedDiscovery: Discovery | null = null;

  async function discovery(signal: AbortSignal): Promise<Discovery> {
    if (cachedDiscovery) return cachedDiscovery;
    if (!options.issuer) throw new Error("AUTHENTIK_ISSUER not configured");
    try {
      const response = await fetch(`${options.issuer}.well-known/openid-configuration`, { signal });
      if (!response.ok) throw new Error(`discovery HTTP ${response.status}`);
      const found: Partial<Discovery> = await response.json();
      if (!found.token_endpoint) throw new Error("discovery returned no token_endpoint");
      cachedDiscovery = { token_endpoint: found.token_endpoint, revocation_endpoint: found.revocation_endpoint };
      return cachedDiscovery;
    } catch (error) {
      // important: only a discovery failure invalidates the cache. Clearing it on a failed grant
      // made the next refresh pay a second round trip, 2.05 s instead of 1.27 s.
      cachedDiscovery = null;
      throw error;
    }
  }

  /** One refresh_token grant. Never rejects: a failure is a result, with what it means. */
  async function grant(refreshToken: string): Promise<GrantResult> {
    const started = options.now();
    try {
      const signal = AbortSignal.timeout(options.timeoutMs);
      const { token_endpoint: tokenEndpoint } = await discovery(signal);
      const response = await fetch(tokenEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          client_id: options.clientId,
          client_secret: options.clientSecret,
          grant_type: "refresh_token",
          refresh_token: refreshToken,
        }),
        signal,
      });
      const body: { access_token?: unknown; refresh_token?: unknown; expires_in?: unknown; error?: unknown } =
        await response.json().catch(() => ({}));
      if (!response.ok || typeof body.access_token !== "string") {
        throw new GrantFailure(response.status, typeof body.error === "string" ? body.error : undefined);
      }
      return {
        ok: true,
        tokens: {
          accessToken: body.access_token,
          refreshToken: typeof body.refresh_token === "string" ? body.refresh_token : refreshToken,
          expiresIn: Number(body.expires_in) || 0,
        },
      };
    } catch (error) {
      const answered = error instanceof GrantFailure;
      const status = answered ? error.status : undefined;
      const oauthError = answered ? error.code : undefined;
      // A timeout or a dropped connection has no OAuth code; its exception name says which it was.
      const named = answered ? oauthError : error instanceof Error ? error.name : undefined;
      const { kind, level } = classifyRefreshFailure(status, oauthError);
      options.log(
        level,
        `[AUTH] refresh failed runtime=${options.runtime} kind=${kind} status=${status ?? "none"} ` +
          `error=${named && ERROR_CODE.test(named.toLowerCase()) ? named : "other"} ms=${options.now() - started}`,
      );
      return { ok: false, kind };
    }
  }

  // RFC 7009: a signed-out refresh token is revoked at the IdP, so no copy of the cookie can use it.
  // note: a 200 does not prove the token existed (RFC 7009 answers 200 for unknown tokens).
  async function revoke(refreshToken: string): Promise<boolean> {
    try {
      const signal = AbortSignal.timeout(5_000);
      const { revocation_endpoint: revocationEndpoint } = await discovery(signal);
      if (!revocationEndpoint) {
        options.log("warn", "[AUTH] the IdP advertises no revocation_endpoint; a signed-out refresh token stays valid there");
        return false;
      }
      const response = await fetch(revocationEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          client_id: options.clientId,
          client_secret: options.clientSecret,
          token: refreshToken,
          token_type_hint: "refresh_token",
        }),
        signal,
      });
      if (!response.ok) options.log("warn", `[AUTH] refresh token revocation answered ${response.status}`);
      return response.ok;
    } catch (error) {
      options.log("warn", `[AUTH] refresh token revocation failed: ${error instanceof Error ? error.name : "unknown"}`);
      return false;
    }
  }

  return { grant, revoke };
}
