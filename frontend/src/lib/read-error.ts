import { APIError } from "@/types/api";

export type ReadErrorKind = "rate-limited" | "timeout" | "unreachable" | "forbidden" | "failed";

export interface ReadError {
  kind: ReadErrorKind;
  /** A short clause that completes "X could not be read: ..." */
  reason: string;
  retryAfterSeconds?: number;
}

/**
 * One reading of a failed request, for every control that has to say why it is empty.
 * Keeps the wording in one place so a pool picker and a network picker cannot drift.
 */
export function describeReadError(error: unknown): ReadError {
  if (error instanceof APIError) {
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
