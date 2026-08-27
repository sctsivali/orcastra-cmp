"use client";

import { AlertTriangle } from "lucide-react";
import { useRelativeTime } from "@/hooks/use-relative-time";
import { cn, formatClockTime } from "@/lib/utils";

export interface LiveTimestampProps {
  /** When the reading was taken. Prefer the payload's collected_at over a client fetch time. */
  updatedAt: number | null | undefined;
  /** Age past which this stops being a claim about now. Use STALE_AFTER_MS for the tier. */
  staleAfterMs?: number;
  /** Overrides the age check: the cluster is not answering, or the row has left the list. */
  forceStale?: boolean;
  /** One clause naming the cause, ex; "nuc2 did not answer the last reachability check". */
  reason?: string;
  className?: string;
}

/**
 * How old the number beside it is.
 *
 * Fresh renders as one muted line with no icon and no colour, which is the same thing an
 * operator already reads next to every RefreshButton, so it costs no new vocabulary and
 * disappears into the chrome. It only becomes visible when the reading has gone cold.
 *
 * important: no refresh control here. Detail panels get liveness from their own poll; adding
 * a second control beside the section one would leave an operator guessing which is which.
 *
 * note: the ticking line is aria-hidden with a status sibling that has content only when
 * stale, so a screen reader is told once that the reading is cold rather than being read the
 * second hand. useRelativeTime already pauses while the tab is hidden.
 */
export function LiveTimestamp({
  updatedAt,
  staleAfterMs,
  forceStale = false,
  reason,
  className,
}: LiveTimestampProps) {
  const relative = useRelativeTime(updatedAt);

  if (!updatedAt) {
    // An unknown age must read as unknown. A body cached before collected_at existed carries
    // no stamp, and rendering that as "just now" is the failure this component exists to stop.
    return (
      <p className={cn("text-xs tabular-nums text-muted-foreground", className)}>
        Age unknown
      </p>
    );
  }

  const isStale =
    forceStale || (staleAfterMs !== undefined && Date.now() - updatedAt > staleAfterMs);

  if (!isStale) {
    return (
      <p
        className={cn("text-xs tabular-nums text-muted-foreground", className)}
        title={`Collected ${formatClockTime(updatedAt)}`}
      >
        Updated {relative}
      </p>
    );
  }

  return (
    <>
      <p
        aria-hidden
        className={cn(
          "flex items-center gap-1.5 text-xs tabular-nums text-foreground",
          className,
        )}
        title={`Collected ${formatClockTime(updatedAt)}`}
      >
        <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-status-pending" />
        <span>
          Last updated {relative}
          {reason ? ` · ${reason}` : ""}
        </span>
      </p>
      <span className="sr-only" role="status">
        {`These readings are stale. Last updated ${relative}${reason ? `. ${reason}` : ""}`}
      </span>
    </>
  );
}
