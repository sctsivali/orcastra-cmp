"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@/hooks/use-toast";

// A refresh that resolves in 80ms is invisible, so hold the pending state open.
// 500ms registers as feedback without reading as an artificial delay.
const MIN_PENDING_MS = 500;
// How long the success tick stays before the control returns to idle.
const SUCCESS_HOLD_MS = 1200;
// Under this, an elapsed counter is noise. Over it, silence reads as a hang.
const ELAPSED_VISIBLE_AFTER_MS = 3000;

export interface UseManualRefreshOptions {
  /** Toast title on failure. */
  errorTitle?: string;
  /** Overrides the error message pulled off the thrown error. */
  errorDescription?: string;
  /** Extra failure handling, called before the toast. */
  onError?: (error: unknown) => void;
}

export interface UseManualRefreshResult {
  /** This control's own request is in flight. Never reflects background polling. */
  pending: boolean;
  /** Briefly true after a successful run, to drive a confirmation tick. */
  succeeded: boolean;
  /** Whole seconds elapsed, non-null only once the run is visibly slow. */
  elapsedSeconds: number | null;
  run: () => Promise<void>;
}

/**
 * The interaction contract for a user-triggered refresh: a double-click guard, a
 * pending state visible long enough to read, a success confirmation, an elapsed
 * counter for slow fan-out endpoints, and an error toast. Call sites supply only
 * the async work.
 *
 * important: pending is scoped to this control's own click. A query's isFetching
 * is also true during background polling, so a button disabled on it disables
 * itself every few seconds and swallows real clicks.
 */
export function useManualRefresh(
  refresh: () => Promise<unknown> | unknown,
  options: UseManualRefreshOptions = {},
): UseManualRefreshResult {
  const [pending, setPending] = useState(false);
  const [succeeded, setSucceeded] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState<number | null>(null);

  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);
  const holdTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const minDelayTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const elapsedTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Keep the latest closures without making `run` unstable.
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (holdTimerRef.current) clearTimeout(holdTimerRef.current);
      if (minDelayTimerRef.current) clearTimeout(minDelayTimerRef.current);
      if (elapsedTimerRef.current) clearInterval(elapsedTimerRef.current);
    };
  }, []);

  const run = useCallback(async () => {
    // important: guard on a ref, not on `pending`. State lands a render late, so
    // a double-click inside one frame would otherwise fire the work twice.
    if (inFlightRef.current) return;
    inFlightRef.current = true;

    const startedAt = Date.now();
    if (holdTimerRef.current) clearTimeout(holdTimerRef.current);
    setPending(true);
    setSucceeded(false);
    setElapsedSeconds(null);

    elapsedTimerRef.current = setInterval(() => {
      const elapsed = Date.now() - startedAt;
      if (elapsed >= ELAPSED_VISIBLE_AFTER_MS && mountedRef.current) {
        setElapsedSeconds(Math.floor(elapsed / 1000));
      }
    }, 1000);

    const stopElapsed = () => {
      if (elapsedTimerRef.current) {
        clearInterval(elapsedTimerRef.current);
        elapsedTimerRef.current = null;
      }
    };

    try {
      await refreshRef.current();

      const remaining = MIN_PENDING_MS - (Date.now() - startedAt);
      if (remaining > 0) {
        await new Promise<void>((resolve) => {
          minDelayTimerRef.current = setTimeout(resolve, remaining);
        });
      }

      stopElapsed();
      if (!mountedRef.current) return;

      setPending(false);
      setElapsedSeconds(null);
      setSucceeded(true);
      holdTimerRef.current = setTimeout(() => {
        if (mountedRef.current) setSucceeded(false);
      }, SUCCESS_HOLD_MS);
    } catch (error) {
      stopElapsed();
      if (mountedRef.current) {
        setPending(false);
        setElapsedSeconds(null);
        setSucceeded(false);
      }

      const {
        errorTitle = "Could not refresh data",
        errorDescription,
        onError,
      } = optionsRef.current;
      onError?.(error);
      toast({
        title: errorTitle,
        description:
          errorDescription ??
          (error instanceof Error && error.message
            ? error.message
            : "Unable to load the latest data. Try again."),
        variant: "destructive",
      });
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  return { pending, succeeded, elapsedSeconds, run };
}
