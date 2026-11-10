/**
 * What an interactive socket (a shell, a serial console, a SPICE display) does after a refused
 * ticket or a close: refresh the session once, sign in again, back off, or stop and say why.
 *
 * A deep link opened in a new tab often holds a session the IdP already ended, so a refused
 * ticket (401 after fetchAPI's own refresh) or a 4401 close forces one session refresh. A new
 * token mints again (at once the first time, then with the backoff); an ended session signs in
 * again (automatically when nothing was live, behind a button otherwise); a silent IdP backs off.
 * 4403, 4428 and 4423 stop and say why. A drop or a 1011 backs off 1, 2, 4, 8, 16 s, then stops.
 * The relay's own endings (1000, 1012, 4404, 4409) stay with the caller, which knows the instance.
 * Not React: the terminal hook and the SPICE session class both hold one.
 */

import { forceSessionRefresh, type SessionRefreshVerdict } from "@/lib/api";
import { reauthenticate } from "@/lib/reauth";
import {
  MAX_RECONNECT_ATTEMPTS,
  SOCKET_MESSAGES,
  backoffMs,
  classifyClose,
  messageFor,
  reconnectingMessage,
  type CloseKind,
} from "@/lib/ws-close";

export type RecoveryStep =
  /** Connect again after `delayMs` (0: at once), showing `message` meanwhile when it is not empty. */
  | { action: "retry"; delayMs: number; message: string }
  /** Final until the operator acts: `signIn` offers Sign In Again, `retry` offers Reconnect. */
  | { action: "stop"; message: string; signIn?: boolean; retry?: boolean }
  /** The page is navigating to sign in again. */
  | { action: "leaving"; message: string }
  /** An ending the caller handles itself (normal, unavailable, taken over, powered down). */
  | { action: "own"; kind: CloseKind };

export interface RecoveryDeps {
  refresh: () => Promise<SessionRefreshVerdict>;
  reauthenticate: (options: { auto: boolean }) => boolean;
  random: () => number;
}

const DEFAULT_RETRY_AFTER_S = 30;

export class SocketRecovery {
  private attempts = 0;
  private authTried = false;

  constructor(private readonly deps: RecoveryDeps = { refresh: forceSessionRefresh, reauthenticate, random: Math.random }) {}

  /** The operator asked to reconnect, or the view mounted: count from the first attempt. */
  restart(): void {
    this.attempts = 0;
    this.authTried = false;
  }

  /**
   * The session is working (a shell's ready, a display's first frame), so its sign-in
   * worked and a later 4401 may refresh again. Attempts still count until stable().
   */
  ready(): void {
    this.authTried = false;
  }

  /** The session stayed up long enough that the next drop counts from the first attempt. */
  stable(): void {
    this.attempts = 0;
  }

  /** Reconnect attempts used since the last restart or stable session. */
  get used(): number {
    return this.attempts;
  }

  private backoff(message?: string): RecoveryStep {
    if (this.attempts >= MAX_RECONNECT_ATTEMPTS) {
      return { action: "stop", message: SOCKET_MESSAGES.gaveUp, retry: true };
    }
    const delayMs = backoffMs(this.attempts, this.deps.random());
    this.attempts += 1;
    return { action: "retry", delayMs, message: message ?? reconnectingMessage(delayMs, this.attempts) };
  }

  private async refreshThenRetry(liveSocket: boolean): Promise<RecoveryStep> {
    if (this.authTried) return { action: "stop", message: SOCKET_MESSAGES.sessionEnded, signIn: true };
    if (this.attempts >= MAX_RECONNECT_ATTEMPTS) return { action: "stop", message: SOCKET_MESSAGES.gaveUp, retry: true };
    this.authTried = true;
    const verdict = await this.deps.refresh();
    if (verdict === "revoked") {
      if (this.deps.reauthenticate({ auto: !liveSocket })) return { action: "leaving", message: SOCKET_MESSAGES.signingInAgain };
      return { action: "stop", message: SOCKET_MESSAGES.sessionEnded, signIn: true };
    }
    if (verdict === "unavailable") {
      // The next round may ask again; the backoff here and the server's floor (auth.ts) bound it.
      this.authTried = false;
      return this.backoff(SOCKET_MESSAGES.idpUnavailable);
    }
    // "refreshed" carries a new token. "unchanged" means the session is as it was: one more
    // ticket settles whether the refusal was the ticket's (spent, expired) or the session's.
    // important: counted like any reconnect, and only the first is immediate. Otherwise a SPICE
    // display whose main channel linked while another was refused 4401 (ex; a ticket store
    // failing over) loops through a refresh, four tickets and a new LXD operation with no wait.
    if (this.attempts === 0) {
      this.attempts = 1;
      return { action: "retry", delayMs: 0, message: "" };
    }
    return this.backoff();
  }

  /** A ticket could not be minted. `liveSocket`: this view had a working session before. */
  async onMintError(error: unknown, { liveSocket }: { liveSocket: boolean }): Promise<RecoveryStep> {
    const status = (error as { status?: number } | null)?.status;
    // The server's own sentence (the break-glass one included) when it gave one.
    const message = error instanceof Error && error.message ? error.message : "Could not open a live connection. Try again.";
    if (status === 401) return this.refreshThenRetry(liveSocket);
    if (status === 429) {
      if (this.attempts >= MAX_RECONNECT_ATTEMPTS) return { action: "stop", message: SOCKET_MESSAGES.gaveUp, retry: true };
      this.attempts += 1;
      const seconds = (error as { retryAfter?: number }).retryAfter ?? DEFAULT_RETRY_AFTER_S;
      return { action: "retry", delayMs: Math.max(1, seconds) * 1000, message };
    }
    // 403 (no access, break-glass), 409 (fenced) and the like are refused again on a retry.
    if (status !== undefined && status >= 400 && status < 500 && status !== 408) return { action: "stop", message };
    // 503 (the ticket store is down), a 5xx, a timeout or no answer at all.
    return this.backoff();
  }

  /**
   * A socket closed. `opened`: it reached its open event. `errorFrame`: the server's error frame
   * before the close, if any. `liveSocket`: this view had a working session before.
   */
  async onClose(
    code: number,
    reason: string,
    { opened, errorFrame, liveSocket }: { opened: boolean; errorFrame?: { code?: number; message?: string } | null; liveSocket: boolean },
  ): Promise<RecoveryStep> {
    const kind = classifyClose(code, reason, { opened, errorFrame, authTried: this.authTried });
    switch (kind) {
      case "auth":
        return this.refreshThenRetry(liveSocket);
      case "forbidden":
      case "break_glass":
      case "fenced":
        return { action: "stop", message: messageFor(kind, reason || errorFrame?.message || "") };
      case "server":
        return this.backoff();
      case "network":
        return this.backoff();
      default:
        return { action: "own", kind };
    }
  }
}
