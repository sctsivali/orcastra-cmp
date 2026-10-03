"use client";

import * as React from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { TaskFeedSnapshot } from "./use-task-feed";

export type LiveState =
  | "connecting"
  | "connected"
  | "degraded"
  | "journal-paused"
  | "reconnecting"
  | "polling"
  | "paused"
  | "offline";

interface Presentation {
  dot: string;
  text: string;
  tooltip: string;
}

// The dot sits on the --status-* tokens, the same four the cluster status dots use. The only
// motion is on "reconnecting", where the value genuinely is changing, and the keyframe is the
// house one behind the reduced-motion guard in globals.css.
function present(state: LiveState, nextRetry: number | null): Presentation {
  switch (state) {
    case "connected":
      return { dot: "bg-status-online", text: "Live", tooltip: "Live updates are connected." };
    case "degraded":
      return {
        dot: "bg-status-pending",
        text: "Degraded",
        tooltip: "Some hypervisor sources are unavailable. Tasks and logs may be incomplete or stale.",
      };
    case "journal-paused":
      return {
        dot: "bg-status-pending",
        text: "Journal paused",
        tooltip:
          "No backend worker is following the clusters right now, so new tasks may be missing until one takes over.",
      };
    case "reconnecting":
      return {
        dot: "bg-status-pending animate-pulse-gentle",
        text: "Reconnecting",
        tooltip:
          nextRetry === null
            ? "Live updates lost, reconnecting."
            : `Live updates lost, reconnecting in ${nextRetry}s.`,
      };
    case "polling":
      return {
        dot: "bg-status-pending",
        text: "Polling",
        tooltip: "Live updates unavailable, polling every 10 seconds.",
      };
    case "paused":
      return {
        dot: "bg-status-unknown",
        text: "Paused",
        tooltip: "Paused while this tab was in the background, resumes when you return.",
      };
    case "offline":
      return { dot: "bg-status-offline", text: "Offline", tooltip: "Task data cannot be refreshed from the dashboard API." };
    default:
      return {
        dot: "bg-status-pending animate-pulse-gentle",
        text: "Connecting",
        tooltip: "Connecting to live updates.",
      };
  }
}

/** Folds the feed snapshot and the API's reachability into the one state the strip shows. */
export function liveStateOf(feed: TaskFeedSnapshot, apiDown: boolean, disconnected = 0): LiveState {
  if (apiDown) return "offline";
  switch (feed.state) {
    case "connected":
      if (feed.followerActive === false) return "journal-paused";
      return disconnected > 0 ? "degraded" : "connected";
    case "reconnecting":
      return "reconnecting";
    case "polling":
      return "polling";
    case "paused":
      return "paused";
    default:
      return "connecting";
  }
}

export function LiveIndicator({ feed, apiDown = false, disconnected = 0 }: {
  feed: TaskFeedSnapshot;
  apiDown?: boolean;
  disconnected?: number;
}) {
  const state = liveStateOf(feed, apiDown, disconnected);
  const { dot, text, tooltip } = present(state, feed.nextRetryInSeconds);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="img"
          aria-label={tooltip}
          tabIndex={0}
          data-live-state={state}
          className="inline-flex items-center gap-1.5 rounded-sm px-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className={cn("h-2 w-2 shrink-0 rounded-full", dot)} />
          <span className="hidden text-xs text-muted-foreground md:inline">{text}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  );
}
