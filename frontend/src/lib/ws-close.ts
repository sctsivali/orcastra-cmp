/**
 * How a terminal or console socket ended, and what the page does next.
 *
 * The codes and reason tokens mirror backend/app/core/ws_close.py (refusals and access closes) and
 * backend/app/services/interactive_relay.py (how a session ended on the instance side).
 * scripts/checks/check-ws-close-contract.mjs keeps the two sides equal and runs the table below.
 *
 * Keep this file free of imports: the check compiles it on its own.
 */

export const WS_CLOSE = {
  /** The shell, console or display ended on the instance side (exit, power off). */
  NORMAL: 1000,
  /** A request no page sends (an unknown console type), or a feed refusing its caller. */
  POLICY_VIOLATION: 1008,
  /** The connection to the instance broke, or the server could not re-check access; reconnecting may work. */
  UPSTREAM_FAILED: 1011,
  /** The instance powered down (stopped, or on its way through a restart). */
  POWERED_DOWN: 1012,
  /** The caller could not be identified: no ticket, a spent or expired one, or an ended session. */
  SESSION_ENDED: 4401,
  /** No access to this instance, at connect or lost while open. It must not reconnect on its own. */
  ACCESS_REVOKED: 4403,
  /** The session cannot start (not running, not found, no agent). Recovery does not retry it, except
   *  that a SPICE display that was on screen waits INSTANCE_RETURN_WINDOW_MS for the instance. */
  UNAVAILABLE: 4404,
  /** Another viewer opened this display (QEMU serves one). Taking it back is the operator's call. */
  TAKEN_OVER: 4409,
  /** Migration Center is moving the instance. */
  FENCED: 4423,
  /** Break-glass access is required, or the grant that opened the session ended. */
  BREAK_GLASS: 4428,
} as const;

/** Every reason token the backend closes with, by code (the relay's own endings carry sentences). */
export const WS_CLOSE_REASONS: Readonly<Record<number, readonly string[]>> = {
  4401: ["auth_required", "ticket_invalid", "session_ended", "token_refused"],
  4403: ["forbidden", "access_revoked:account_ended", "access_revoked:role_changed", "access_revoked:access_ended"],
  4428: ["break_glass_required", "break_glass_expired"],
  4423: ["migration_fenced"],
  1011: ["reauthorize_failed", "upstream_unavailable", "session_failed"],
  1008: ["unknown_console_type"],
};

export type CloseKind =
  | "normal"
  | "auth"
  | "forbidden"
  | "break_glass"
  | "fenced"
  | "unavailable"
  | "taken_over"
  | "powered_down"
  | "server"
  | "network";

export interface CloseContext {
  /** The socket reached its open event (the server accepted it). */
  opened: boolean;
  /** An error frame the server sent before closing, with the code it named. */
  errorFrame?: { code?: number } | null;
  /** This connect already went through the session refresh once. */
  authTried?: boolean;
}

export const MAX_RECONNECT_ATTEMPTS = 5;

export const SOCKET_MESSAGES = {
  signingInAgain: "Your sign-in ended. Signing you in again.",
  sessionEnded: "Your session ended. Sign in again to open this terminal.",
  forbidden: "You do not have access to this instance.",
  accessRevoked: "Access to this instance was revoked.",
  breakGlassRequired: "Break-glass access is required to open a session on this Cluster.",
  breakGlassExpired: "Your break-glass access ended, so the session was closed.",
  fenced: "Migration Center is moving this instance. Try again when the move finishes.",
  unknownConsoleType: "This console type is not supported.",
  reauthorizeFailed: "Your access to this instance could not be checked.",
  gaveUp: "Could not reconnect. Select Reconnect to try again.",
  idpUnavailable: "The sign-in service did not answer. Retrying.",
  newShell: "Reconnected. This is a new shell session.",
} as const;

/** A reason the backend sent as a token rather than a sentence. */
export function isReasonToken(reason: string): boolean {
  return Object.values(WS_CLOSE_REASONS).some((reasons) => reasons.includes(reason));
}

/**
 * What a close means. A dropped connection that carried an error frame first is read by the
 * frame's code. A socket that never opened on its first try is read as an auth refusal once: a
 * backend that refuses before accepting answers HTTP 403, which the browser reports as 1006.
 */
export function classifyClose(code: number, reason: string, context: CloseContext): CloseKind {
  const dropped = code === 1005 || code === 1006;
  const effective = dropped && context.errorFrame?.code ? context.errorFrame.code : code;
  switch (effective) {
    case WS_CLOSE.NORMAL:
      return "normal";
    case WS_CLOSE.SESSION_ENDED:
      return "auth";
    case WS_CLOSE.ACCESS_REVOKED:
    case WS_CLOSE.POLICY_VIOLATION:
      return "forbidden";
    case WS_CLOSE.BREAK_GLASS:
      return "break_glass";
    case WS_CLOSE.FENCED:
      return "fenced";
    case WS_CLOSE.UNAVAILABLE:
      return "unavailable";
    case WS_CLOSE.TAKEN_OVER:
      return "taken_over";
    case WS_CLOSE.POWERED_DOWN:
      return "powered_down";
    case WS_CLOSE.UPSTREAM_FAILED:
      return "server";
    default:
      if (dropped && !context.opened && !context.authTried) return "auth";
      return "network";
  }
}

/** The sentence for a final close. `detail` is the close reason or the error frame's message. */
export function messageFor(kind: CloseKind, detail = ""): string {
  const sentence = detail && !isReasonToken(detail) ? detail : "";
  switch (kind) {
    case "auth":
      return SOCKET_MESSAGES.sessionEnded;
    case "forbidden":
      if (detail.startsWith("access_revoked")) return SOCKET_MESSAGES.accessRevoked;
      if (detail === "unknown_console_type") return SOCKET_MESSAGES.unknownConsoleType;
      return sentence || SOCKET_MESSAGES.forbidden;
    case "break_glass":
      return detail === "break_glass_expired" ? SOCKET_MESSAGES.breakGlassExpired : SOCKET_MESSAGES.breakGlassRequired;
    case "fenced":
      return SOCKET_MESSAGES.fenced;
    case "server":
      if (detail === "reauthorize_failed") return SOCKET_MESSAGES.reauthorizeFailed;
      return sentence || "The connection to the instance broke.";
    case "unavailable":
      return sentence || "The session cannot start.";
    case "taken_over":
      return sentence || "Another viewer opened this console.";
    case "powered_down":
      return sentence || "The instance shut down.";
    case "normal":
      return sentence || "The session ended.";
    case "network":
      return "Connection lost.";
  }
}

/** "Connection lost. Reconnecting in Ns (attempt i of 5)." for the attempt about to wait `delayMs`. */
export function reconnectingMessage(delayMs: number, attempt: number): string {
  return `Connection lost. Reconnecting in ${Math.max(1, Math.round(delayMs / 1000))}s (attempt ${attempt} of ${MAX_RECONNECT_ATTEMPTS}).`;
}

/** The wait before reconnect `attempt` (0-based): 1, 2, 4, 8, 16 s, each within 20% either way. */
export function backoffMs(attempt: number, rnd: number): number {
  const base = 1000 * 2 ** Math.max(0, Math.min(attempt, MAX_RECONNECT_ATTEMPTS - 1));
  const jitter = 0.8 + 0.4 * Math.min(1, Math.max(0, rnd));
  return Math.round(base * jitter);
}
