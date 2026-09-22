import { APIError } from "@/types/api";

export interface UploadTerminalState {
  status: "pending" | "uploading" | "uploaded" | "importing" | "ready" | "failed";
  error: string | null;
}

export interface FollowUploadOptions<T extends UploadTerminalState> {
  signal: AbortSignal;
  readStatus: (signal: AbortSignal) => Promise<T>;
  initialStatus?: T;
  startedAt?: number;
  pollMs?: number;
  maxPollMs?: number;
  maxTransientFailures?: number;
  unknownMessage: string;
}

const DEFAULT_POLL_MS = 2_000;
const DEFAULT_MAX_POLL_MS = 30 * 60_000;
const DEFAULT_MAX_TRANSIENT_FAILURES = 4;

export class UploadTerminalFailure<T extends UploadTerminalState> extends Error {
  constructor(
    message: string,
    public readonly terminalStatus: T,
  ) {
    super(message);
    this.name = "UploadTerminalFailure";
  }
}

export class UploadOutcomeUnknown extends Error {
  constructor(message: string, public readonly source?: unknown) {
    super(message);
    this.name = "UploadOutcomeUnknown";
  }
}

function waitForUploadPoll(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(finish, ms);
    const onAbort = () => {
      cleanup();
      reject(signal.reason ?? new DOMException("Cancelled", "AbortError"));
    };
    const cleanup = () => {
      clearTimeout(timeout);
      signal.removeEventListener("abort", onAbort);
    };
    function finish() {
      cleanup();
      resolve();
    }

    if (signal.aborted) onAbort();
    else signal.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Follow a submitted upload import without ever replaying its finalize request.
 *
 * The caller owns the one finalize attempt. This helper only performs cancellable reads, bounds
 * the total uncertainty window, honours API backpressure, and distinguishes a daemon-reported
 * failure from an outcome that Orcastra could not prove.
 */
export async function followUploadToTerminal<T extends UploadTerminalState>(
  options: FollowUploadOptions<T>,
): Promise<T> {
  const pollMs = options.pollMs ?? DEFAULT_POLL_MS;
  const maxPollMs = options.maxPollMs ?? DEFAULT_MAX_POLL_MS;
  const maxTransientFailures =
    options.maxTransientFailures ?? DEFAULT_MAX_TRANSIENT_FAILURES;
  const startedAt = options.startedAt ?? Date.now();
  const deadlineAt = startedAt + maxPollMs;
  const deadlineController = new AbortController();
  const deadlineError = new UploadOutcomeUnknown(options.unknownMessage);
  const abortFromCaller = () => {
    deadlineController.abort(
      options.signal.reason ?? new DOMException("Cancelled", "AbortError"),
    );
  };

  if (options.signal.aborted) abortFromCaller();
  else options.signal.addEventListener("abort", abortFromCaller, { once: true });

  const remainingAtStart = Math.max(0, deadlineAt - Date.now());
  const deadlineTimer = setTimeout(
    () => deadlineController.abort(deadlineError),
    remainingAtStart,
  );

  let current = options.initialStatus;
  let nextDelay = current ? pollMs : 0;
  let transientFailures = 0;

  try {
    for (;;) {
      if (current?.status === "ready") return current;
      if (current?.status === "failed") {
        throw new UploadTerminalFailure(
          current.error || "Import failed on the cluster",
          current,
        );
      }

      const remaining = deadlineAt - Date.now();
      if (remaining <= 0) throw deadlineError;
      await waitForUploadPoll(Math.min(nextDelay, remaining), deadlineController.signal);

      try {
        current = await options.readStatus(deadlineController.signal);
        transientFailures = 0;
        nextDelay = pollMs;
      } catch (error) {
        if (deadlineController.signal.aborted) {
          throw deadlineController.signal.reason ?? error;
        }
        if (error instanceof APIError && error.status === 429) {
          nextDelay = Math.max(pollMs, (error.retryAfter ?? 30) * 1_000);
          current = undefined;
          continue;
        }
        if (error instanceof APIError && error.status >= 400 && error.status < 500) {
          throw new UploadOutcomeUnknown(options.unknownMessage, error);
        }
        transientFailures += 1;
        if (transientFailures >= maxTransientFailures) {
          throw new UploadOutcomeUnknown(options.unknownMessage, error);
        }
        current = undefined;
        nextDelay = pollMs;
      }
    }
  } catch (error) {
    if (error === deadlineError) throw deadlineError;
    if (error instanceof UploadTerminalFailure || error instanceof UploadOutcomeUnknown) {
      throw error;
    }
    if (options.signal.aborted) {
      throw options.signal.reason ?? error;
    }
    if (deadlineController.signal.aborted && deadlineController.signal.reason === deadlineError) {
      throw deadlineError;
    }
    throw error;
  } finally {
    clearTimeout(deadlineTimer);
    options.signal.removeEventListener("abort", abortFromCaller);
  }
}
