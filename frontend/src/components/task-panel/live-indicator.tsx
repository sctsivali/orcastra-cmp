"use client";

import * as React from "react";
import { Activity } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { badgeVariants, type BadgeTone } from "@/components/ui/badge";
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
  tone: BadgeTone;
  text: string;
  tooltip: string;
}

// A solid chip on the shared state tones from md up, so the word is visible and colour only
// reinforces it; the accessible name is the tooltip text at every width.
const TONE_ICON: Record<BadgeTone, string> = {
  positive: "text-status-online",
  caution: "text-status-pending",
  neutral: "text-status-unknown",
  negative: "text-status-offline",
};

function present(state: LiveState, nextRetry: number | null): Presentation {
  switch (state) {
    case "connected":
      return { tone: "positive", text: "Live", tooltip: "Live updates are connected." };
    case "degraded":
      return {
        tone: "caution",
        text: "Degraded",
        tooltip: "Task collection is interrupted on some clusters. Tasks and logs may be incomplete or stale.",
      };
    case "journal-paused":
      return {
        tone: "caution",
        text: "Journal paused",
        tooltip:
          "No backend worker is following the clusters right now, so new tasks may be missing until one takes over.",
      };
    case "reconnecting":
      return {
        tone: "caution",
        text: "Reconnecting",
        tooltip:
          nextRetry === null
            ? "Live updates lost, reconnecting."
            : `Live updates lost, reconnecting in ${nextRetry}s.`,
      };
    case "polling":
      return {
        tone: "caution",
        text: "Polling",
        tooltip: "Live updates unavailable, polling every 10 seconds.",
      };
    case "paused":
      return {
        tone: "neutral",
        text: "Paused",
        tooltip: "Paused while this tab was in the background, resumes when you return.",
      };
    case "offline":
      return { tone: "negative", text: "Offline", tooltip: "Task data cannot be refreshed from the dashboard API." };
    default:
      return {
        tone: "caution",
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
  const { tone, text, tooltip } = present(state, feed.nextRetryInSeconds);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="img"
          aria-label={tooltip}
          tabIndex={0}
          data-live-state={state}
          className="inline-flex shrink-0 items-center rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {/* The strip has no room for a word on a phone: the chip would push the refresh and
              expand controls off screen at 390px. Below md the state is a toned icon. */}
          <Activity className={cn("h-4 w-4 md:hidden", TONE_ICON[tone])} aria-hidden="true" />
          <span className={cn(badgeVariants({ variant: tone }), "hidden px-2 md:inline-flex")}>{text}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  );
}
