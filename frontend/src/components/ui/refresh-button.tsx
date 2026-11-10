"use client";

import { Check, RefreshCw } from "lucide-react";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import { useRelativeTime } from "@/hooks/use-relative-time";
import { cn, formatClockTime, lowerFirst } from "@/lib/utils";

export interface RefreshButtonProps
  extends Omit<ButtonProps, "children" | "onClick"> {
  /** The refresh work. Must reject on failure so the error can be surfaced. */
  onRefresh: () => Promise<unknown> | unknown;
  /** Accessible name and tooltip verb, ex; "Refresh clusters". */
  label?: string;
  /**
   * React Query's dataUpdatedAt. Renders a live "Updated 5s ago" beside the
   * icon, so an operator can confirm the click landed.
   */
  lastUpdatedAt?: number | null;
  /** Toast title used when the refresh fails. */
  errorTitle?: string;
  /** Keeps the timestamp out of the layout where there is no room for it; the tooltip still says when. */
  hideTimestamp?: boolean;
  iconClassName?: string;
}

/**
 * The single refresh control for the console.
 *
 * It owns its pending state instead of accepting one. A query's isFetching is
 * also true during background polling, so a button bound to it would disable
 * itself every few seconds and swallow clicks.
 */
export function RefreshButton({
  className,
  errorTitle,
  hideTimestamp = false,
  iconClassName,
  label = "Refresh",
  lastUpdatedAt,
  onRefresh,
  size = "icon",
  type = "button",
  variant = "outline",
  disabled,
  ...props
}: RefreshButtonProps) {
  const { pending, succeeded, elapsedSeconds, run } = useManualRefresh(
    onRefresh,
    { errorTitle: errorTitle ?? `Could not ${lowerFirst(label)}` },
  );

  const relative = useRelativeTime(lastUpdatedAt);
  const showTimestamp = !hideTimestamp && !!lastUpdatedAt;

  const statusText = pending
    ? elapsedSeconds === null
      ? "Refreshing…"
      : `Refreshing… ${elapsedSeconds}s`
    : `Updated ${relative}`;

  // Keyed on the timestamp, not on showTimestamp, so a compact control with no room for
  // the label (the task dock) still answers "when" on hover and focus.
  const tooltip = pending
    ? "Refreshing…"
    : lastUpdatedAt
      ? `Last updated ${formatClockTime(lastUpdatedAt)} · ${label}`
      : label;

  const control = (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-busy={pending}
          aria-label={label}
          className={cn("shrink-0", size === "icon" && "h-9 w-9", className)}
          disabled={disabled || pending}
          onClick={() => void run()}
          size={size}
          title={label}
          type={type}
          variant={variant}
          {...props}
        >
          {succeeded ? (
            <Check className={cn("h-4 w-4 text-success", iconClassName)} />
          ) : (
            <RefreshCw
              className={cn(
                "h-4 w-4",
                // motion-reduce keeps the colour cue when the spin is suppressed,
                // so the pending state never becomes invisible
                pending && "animate-spin text-primary motion-reduce:animate-none",
                iconClassName,
              )}
            />
          )}
          <span aria-live="polite" className="sr-only" role="status">
            {pending ? "Refreshing" : succeeded ? "Refreshed" : ""}
          </span>
        </Button>
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  );

  if (!showTimestamp) return control;

  return (
    <div className="flex items-center gap-2">
      <span
        className={cn(
          "hidden text-xs tabular-nums text-muted-foreground sm:inline",
          pending && "text-foreground",
        )}
      >
        {statusText}
      </span>
      {control}
    </div>
  );
}
