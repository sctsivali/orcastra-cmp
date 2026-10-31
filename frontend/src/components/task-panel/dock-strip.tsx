"use client";

import { plural } from "@/lib/utils";
import * as React from "react";
import { AlertTriangle, ArrowUpRight, ChevronDown, ChevronUp } from "lucide-react";
import { IntentLink } from "@/components/navigation/route-intent";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { DockTab } from "@/lib/dock-cookie";
import { inventoryLinks } from "@/lib/inventory-links";
import { LiveIndicator } from "./live-indicator";
import type { TaskFeedSnapshot } from "./use-task-feed";

export interface DockStripProps {
  open: boolean;
  tab: DockTab;
  running: number;
  failedRecent: number;
  /** Pending plus running across the queue, once the queue has been read. */
  queued: number | null;
  disconnected: string[];
  feed: TaskFeedSnapshot;
  apiDown: boolean;
  onToggle: () => void;
  /** A trigger clicked while collapsed: open on that tab. */
  onExpandTo: (tab: DockTab) => void;
  refreshNow: () => Promise<unknown>;
  /** When the task summary last answered; the strip has no room for it, so the tooltip says it. */
  lastUpdatedAt: number;
  /** Partner and above: the cluster-naming notice and the Operations link. */
  showOperator: boolean;
  caretRef: React.RefObject<HTMLButtonElement>;
}

// note: globals.css forces every button to 44 px below md, so the list and its triggers take
// the strip's full height there and only shrink to the 28 px row from md up.
const TRIGGER_CLASS = "shrink-0 px-2 py-0 text-xs md:h-7";
const COUNT_CLASS = "ml-1.5 px-1.5 py-0 tabular-nums";

// The strip is the whole panel when collapsed: 44 px below md, where globals.css forces every
// button to that height, 36 px from md up. Presentational; every decision lives in TaskPanel.
export function DockStrip({
  open,
  tab,
  running,
  failedRecent,
  queued,
  disconnected,
  feed,
  apiDown,
  onToggle,
  onExpandTo,
  refreshNow,
  lastUpdatedAt,
  showOperator,
  caretRef,
}: DockStripProps) {
  const expandIfCollapsed = (next: DockTab) => {
    if (!open) onExpandTo(next);
  };

  return (
    <div className="flex h-11 shrink-0 items-center gap-2 border-b bg-background/95 px-2 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:h-9">
      <Button
        ref={caretRef}
        type="button"
        variant="ghost"
        size="icon"
        className="h-7 w-7 shrink-0"
        aria-label={open ? "Collapse task panel" : "Expand task panel"}
        aria-expanded={open}
        aria-controls="task-panel-body"
        onClick={onToggle}
      >
        {open ? <ChevronDown className="h-4 w-4" aria-hidden="true" /> : <ChevronUp className="h-4 w-4" aria-hidden="true" />}
      </Button>

      {/* min-w-0 lets the tabs give way on a phone. With the default min-width the tab counts
          pushed the refresh, expand and operator controls off a 390px screen. */}
      <TabsList className="h-11 min-w-0 justify-start overflow-x-auto p-0 md:h-8 md:p-0.5" aria-label="Task panel tabs">
        <TabsTrigger value="tasks" className={TRIGGER_CLASS} onClick={() => expandIfCollapsed("tasks")}>
          Tasks
          {running > 0 && (
            <Badge variant="caution" asChild className={COUNT_CLASS}>
              <span aria-label={`${running} in progress`}>{running}</span>
            </Badge>
          )}
          {failedRecent > 0 && (
            <Badge variant="negative" asChild className={COUNT_CLASS}>
              <span aria-label={`${failedRecent} failed in the last 15 minutes`}>{failedRecent}</span>
            </Badge>
          )}
        </TabsTrigger>
        <TabsTrigger value="queue" className={TRIGGER_CLASS} onClick={() => expandIfCollapsed("queue")}>
          Queue
          {queued !== null && queued > 0 && (
            <Badge variant="neutral" asChild className={COUNT_CLASS}>
              <span aria-label={`${queued} in the queue`}>{queued}</span>
            </Badge>
          )}
        </TabsTrigger>
        <TabsTrigger value="log" className={TRIGGER_CLASS} onClick={() => expandIfCollapsed("log")}>
          Cluster Log
        </TabsTrigger>
      </TabsList>

      <div className="flex-1" />

      {disconnected.length > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-7 w-7 shrink-0 text-status-pending"
              aria-label={`Live task updates interrupted on ${plural(disconnected.length, "cluster")}`}
              onClick={() => onExpandTo("queue")}
            >
              <AlertTriangle className="h-4 w-4" aria-hidden="true" />
            </Button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">
            {showOperator
              ? `Task collection is interrupted on ${disconnected.join(", ")}.`
              : `Task collection is interrupted on ${plural(disconnected.length, "cluster")}.`}
            {" "}Tasks and logs may be incomplete or stale until collection recovers.
          </TooltipContent>
        </Tooltip>
      )}

      <LiveIndicator feed={feed} apiDown={apiDown} disconnected={disconnected.length} />

      <RefreshButton
        label={tab === "log" && open ? "Refresh log" : tab === "queue" && open ? "Refresh queue" : "Refresh tasks"}
        hideTimestamp
        lastUpdatedAt={lastUpdatedAt}
        variant="ghost"
        className="h-7 w-7 shrink-0"
        onRefresh={refreshNow}
      />

      {showOperator && (
        <Button asChild variant="ghost" size="icon" className="h-7 w-7 shrink-0">
          <IntentLink href={inventoryLinks.operations({})} aria-label="Open the Operations page">
            <ArrowUpRight className="h-4 w-4" aria-hidden="true" />
          </IntentLink>
        </Button>
      )}
    </div>
  );
}
