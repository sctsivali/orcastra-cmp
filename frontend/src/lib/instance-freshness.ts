export const INSTANCE_SAFETY_READ_MS = 30_000;
export const INSTANCE_OPEN_RESYNC_COALESCE_MS = 100;

export interface InstanceFreshnessClock {
  now: () => number;
  setTimeout: (callback: () => void | Promise<void>, delayMs: number) => unknown;
  clearTimeout: (timer: unknown) => void;
}

export interface InstanceReconcileRateLimitState {
  queuedFresh: boolean;
  retryAt: number;
}

export interface InstanceFreshnessRequest {
  actorScope: string;
  fresh: boolean;
}

interface RateLimitClassification {
  retryAfterSeconds?: number;
}

interface InstanceFreshnessCoordinatorOptions {
  actorScope: string;
  isVisible: () => boolean;
  areAllStreamsHealthy: () => boolean;
  reconcile: (request: InstanceFreshnessRequest) => Promise<void>;
  classifyRateLimit: (error: unknown) => RateLimitClassification | null;
  retainRateLimit: (
    queuedFresh: boolean,
    requestedFresh: boolean,
    retryAvailable: boolean,
    retryAfterSeconds: number | undefined,
    now: number,
  ) => InstanceReconcileRateLimitState;
  clock?: InstanceFreshnessClock;
  safetyReadMs?: number;
  openResyncCoalesceMs?: number;
}

const browserClock: InstanceFreshnessClock = {
  now: () => Date.now(),
  setTimeout: (callback, delayMs) => setTimeout(callback, delayMs),
  clearTimeout: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
};

/** Keep every authorized cluster, even when its inventory currently has zero rows. */
export function getInstanceStreamHosts(
  authorizedHosts: string[],
  pendingHosts: string[],
): string[] {
  return Array.from(new Set([...authorizedHosts, ...pendingHosts])).filter(Boolean);
}

/**
 * Actor-scoped fleet reconciliation state machine.
 *
 * The coordinator owns no React, DOM, WebSocket, or query-cache state. Callers provide
 * visibility, stream health, error classification, and the authoritative read, so cadence and
 * Retry-After behavior stay deterministic under a fake clock.
 */
export function createInstanceFreshnessCoordinator(
  options: InstanceFreshnessCoordinatorOptions,
) {
  const clock = options.clock ?? browserClock;
  const safetyReadMs = options.safetyReadMs ?? INSTANCE_SAFETY_READ_MS;
  const coalesceMs = options.openResyncCoalesceMs ?? INSTANCE_OPEN_RESYNC_COALESCE_MS;

  let active = false;
  let wasVisible = false;
  let safetyTimer: unknown;
  let reconcileTimer: unknown;
  let reconcileInFlight = false;
  let queuedFresh = false;
  let queuedFreshRetryAvailable = false;
  let rateLimitedUntil = 0;

  const clearSafetyTimer = () => {
    if (safetyTimer === undefined) return;
    clock.clearTimeout(safetyTimer);
    safetyTimer = undefined;
  };

  const scheduleReconcile = (
    fresh: boolean,
    delayMs = coalesceMs,
    allowRateLimitRetry = true,
  ) => {
    if (!active || !options.isVisible()) return;
    queuedFresh ||= fresh;
    if (fresh && allowRateLimitRetry) queuedFreshRetryAvailable = true;
    if (reconcileTimer !== undefined || reconcileInFlight) return;

    reconcileTimer = clock.setTimeout(async () => {
      reconcileTimer = undefined;
      if (!active || !options.isVisible()) return;

      if (clock.now() < rateLimitedUntil) {
        // Only an authoritative intent survives Retry-After. Requeue it once without granting
        // the delayed retry another self-retry allowance.
        if (queuedFresh) {
          scheduleReconcile(
            true,
            Math.max(1, rateLimitedUntil - clock.now()),
            false,
          );
        }
        return;
      }

      const requestedFresh = queuedFresh;
      const retryAvailable = queuedFreshRetryAvailable;
      queuedFresh = false;
      queuedFreshRetryAvailable = false;
      reconcileInFlight = true;
      try {
        await options.reconcile({
          actorScope: options.actorScope,
          fresh: requestedFresh,
        });
      } catch (error) {
        const rateLimit = options.classifyRateLimit(error);
        if (rateLimit) {
          const retry = options.retainRateLimit(
            queuedFresh,
            requestedFresh,
            retryAvailable,
            rateLimit.retryAfterSeconds,
            clock.now(),
          );
          queuedFresh = retry.queuedFresh;
          rateLimitedUntil = retry.retryAt;
        }
        // Keep the last-good rows. A safety tick, reconnect, or the single bounded
        // Retry-After attempt is the next read boundary.
      } finally {
        reconcileInFlight = false;
        if (queuedFresh && active) {
          scheduleReconcile(
            true,
            Math.max(coalesceMs, rateLimitedUntil - clock.now()),
            false,
          );
        }
      }
    }, Math.max(0, delayMs));
  };

  const armSafetyTimer = () => {
    clearSafetyTimer();
    if (!active || !options.isVisible()) return;
    safetyTimer = clock.setTimeout(() => {
      safetyTimer = undefined;
      if (!active || !options.isVisible()) return;
      scheduleReconcile(!options.areAllStreamsHealthy(), 0);
      armSafetyTimer();
    }, safetyReadMs);
  };

  const start = () => {
    if (active) return;
    active = true;
    wasVisible = options.isVisible();
    armSafetyTimer();
  };

  const stop = () => {
    active = false;
    clearSafetyTimer();
    if (reconcileTimer !== undefined) {
      clock.clearTimeout(reconcileTimer);
      reconcileTimer = undefined;
    }
    queuedFresh = false;
    queuedFreshRetryAvailable = false;
  };

  const visibilityChanged = () => {
    const visible = options.isVisible();
    const resumed = !wasVisible && visible;
    wasVisible = visible;
    if (!visible) {
      clearSafetyTimer();
      return;
    }
    if (resumed) {
      scheduleReconcile(true, 0);
      armSafetyTimer();
    } else if (safetyTimer === undefined) {
      armSafetyTimer();
    }
  };

  return {
    start,
    stop,
    requestReconcile: (fresh: boolean) => scheduleReconcile(fresh),
    streamOpened: () => scheduleReconcile(true),
    visibilityChanged,
  };
}
