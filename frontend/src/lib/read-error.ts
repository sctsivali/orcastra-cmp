import { APIError } from "@/types/api";

export type ReadErrorKind = "rate-limited" | "timeout" | "unreachable" | "forbidden" | "failed";

export interface ReadError {
  kind: ReadErrorKind;
  /** A short clause that completes "X could not be read: ..." */
  reason: string;
  retryAfterSeconds?: number;
}

const AUTHORITY_READ_ERRORS = {
  identity_binding_required: {
    status: 403,
    kind: "forbidden",
    reason: "this sign-in could not be bound to a current identity; sign out, sign in again, then retry",
  },
  current_role_denied: {
    status: 403,
    kind: "forbidden",
    reason: "your current role does not allow Migration Center access; ask an administrator to assign the Admin or Partner role",
  },
  authority_unavailable: {
    status: 503,
    kind: "failed",
    reason: "current access could not be verified; retry shortly or ask an administrator to check the identity service",
  },
  identity_provider_unavailable: {
    status: 503,
    kind: "failed",
    reason: "the sign-in service could not be reached to verify this session; retry shortly",
  },
} as const satisfies Record<
  string,
  { status: number; kind: ReadErrorKind; reason: string }
>;

type AuthorityReadErrorCode = keyof typeof AUTHORITY_READ_ERRORS;

function describeAuthorityReadError(error: APIError): ReadError | undefined {
  if (
    typeof error.detail !== "object" ||
    error.detail === null ||
    Array.isArray(error.detail)
  ) {
    return undefined;
  }

  const code = (error.detail as Record<string, unknown>).error;
  if (typeof code !== "string" || !(code in AUTHORITY_READ_ERRORS)) {
    return undefined;
  }

  const description = AUTHORITY_READ_ERRORS[code as AuthorityReadErrorCode];
  if (error.status !== description.status) return undefined;

  // Keep copy local and allowlisted. A server-provided message must never turn an upstream
  // identity diagnostic into browser-visible text.
  return { kind: description.kind, reason: description.reason };
}

/**
 * One reading of a failed request, for every control that has to say why it is empty.
 * Keeps the wording in one place so a pool picker and a network picker cannot drift.
 */
export function describeReadError(error: unknown): ReadError {
  if (error instanceof APIError) {
    const authorityError = describeAuthorityReadError(error);
    if (authorityError) return authorityError;

    if (error.status === 429) {
      const wait = error.retryAfter ?? 30;
      return { kind: "rate-limited", reason: `rate limited by the API, try again in ${wait} s`, retryAfterSeconds: wait };
    }
    if (error.status === 504 || /timeout/i.test(error.message)) {
      return { kind: "timeout", reason: "the request timed out" };
    }
    if (error.status === 0) {
      return { kind: "unreachable", reason: "the console cannot reach the API" };
    }
    if (error.status === 401 || error.status === 403) {
      return { kind: "forbidden", reason: "you are not allowed to read this" };
    }
    if (error.status >= 500) {
      return { kind: "failed", reason: typeof error.detail === "string" && error.detail ? error.detail : `the API answered ${error.status}` };
    }
    return { kind: "failed", reason: error.message || `HTTP ${error.status}` };
  }
  if (error instanceof Error && error.message) {
    return { kind: "failed", reason: error.message };
  }
  return { kind: "failed", reason: "an unknown error" };
}

/**
 * Whether a failed request means Vault is not available, as opposed to any other 503.
 *
 * important: every 503 used to read as "Vault is not configured", so an identity-provider outage
 * replaced the Integrations page with Vault setup instructions. Every Vault 503 the backend writes
 * names Vault in its detail; nothing else does. Duck-typed, because `instanceof` breaks across
 * Next chunks.
 */
/**
 * The retry rule for a read whose 503 will not clear within a retry (Vault missing, or the sign-in
 * service down). Every observer of one query key must pass the same rule, because react-query runs
 * a shared query with whichever observer's options it holds.
 */
export function retryUnlessUnavailable(failureCount: number, error: unknown): boolean {
  if (error instanceof Error && "status" in error && (error as { status?: unknown }).status === 503) return false;
  return failureCount < 2;
}

export function isVaultUnavailable(error: unknown): boolean {
  if (!(error instanceof Error) || !("status" in error)) return false;
  const { status, detail } = error as unknown as { status?: unknown; detail?: unknown };
  return status === 503 && typeof detail === "string" && /\bvault\b/i.test(detail);
}
