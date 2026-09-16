"use client";

import * as React from "react";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";

import { cn } from "@/lib/utils";
import type { PickerNotice as PickerNoticeState } from "@/lib/picker-state";

/** Seconds left until `seconds` from mount have passed; restarts when `seconds` changes. */
export function useRetryCountdown(seconds: number | undefined): number {
  const [left, setLeft] = React.useState(seconds ?? 0);
  React.useEffect(() => {
    setLeft(seconds ?? 0);
    if (!seconds || seconds <= 0) return;
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      const remaining = Math.max(0, seconds - Math.floor((Date.now() - startedAt) / 1000));
      setLeft(remaining);
      if (remaining <= 0) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [seconds]);
  return left;
}

interface PickerNoticeProps {
  id?: string;
  notice?: PickerNoticeState;
  onRetry?: () => void | Promise<unknown>;
  className?: string;
}

/**
 * The line under a data-driven picker that says why it is not offering anything: reading,
 * failed with the reason and a Retry, or rate limited with a countdown. A disabled trigger is
 * not focusable, so this is where the reason and the action live, as a live region rather
 * than a tooltip.
 */
export function PickerNotice({ id, notice, onRetry, className }: PickerNoticeProps) {
  const [retrying, setRetrying] = React.useState(false);
  const left = useRetryCountdown(notice?.kind === "rate-limited" ? notice.retryAfterSeconds : undefined);

  if (!notice) return null;

  const rateLimited = notice.kind === "rate-limited";
  const waiting = rateLimited && left > 0;
  const text = rateLimited
    ? waiting
      ? notice.text.replace(/\d+ s\./, `${left} s.`)
      : notice.text.replace(/, try again in \d+ s\./, ". Retry when ready.")
    : notice.text;

  const handleRetry = async () => {
    if (!onRetry || retrying || waiting) return;
    setRetrying(true);
    try {
      await onRetry();
    } finally {
      setRetrying(false);
    }
  };

  if (notice.kind === "loading") {
    return (
      <p id={id} role="status" aria-live="polite" className={cn("flex items-start gap-2 text-xs text-muted-foreground", className)}>
        <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden />
        <span>{text}</span>
      </p>
    );
  }

  return (
    <p id={id} role="status" aria-live="polite" className={cn("flex items-start gap-2 text-xs", className)}>
      <AlertTriangle
        className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", rateLimited ? "text-status-pending" : "text-destructive")}
        aria-hidden
      />
      <span className={rateLimited ? "text-muted-foreground" : "text-destructive"}>
        {text}
        {onRetry && (
          <button
            type="button"
            onClick={handleRetry}
            disabled={retrying || waiting}
            className="ml-1.5 inline-flex items-center gap-1 underline decoration-dotted underline-offset-2 hover:decoration-solid disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3 w-3", retrying && "animate-spin")} aria-hidden />
            {retrying ? "Retrying" : "Retry"}
          </button>
        )}
      </span>
    </p>
  );
}
