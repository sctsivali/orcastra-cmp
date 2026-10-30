"use client";

import * as React from "react";
import { Upload } from "lucide-react";
import { Progress } from "@/components/ui/progress";
import { hasDrafts } from "@/lib/instance-files/draft-store";
import { restoreInterrupted } from "@/lib/instance-files/transfer-pump";
import { hasActiveTransfers, useTransferStore } from "@/lib/instance-files/transfer-store";
import { formatNetworkRate } from "@/lib/utils";
import { summarizeTransfers } from "./transfer-panel";

/**
 * Uploads outlive whichever explorer started them, so this stays on screen wherever the person
 * goes, and is the one place that warns before a reload would lose their place.
 */
export function TransferTray() {
  const transfers = useTransferStore((state) => state.transfers);
  const { active, percent, rate } = summarizeTransfers(transfers);

  React.useEffect(() => {
    restoreInterrupted();
    const warn = (event: BeforeUnloadEvent) => {
      if (hasActiveTransfers() || hasDrafts()) event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  if (active.length === 0) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed bottom-3 left-3 z-[99999] flex w-64 items-center gap-2 rounded-md border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md"
    >
      <Upload className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="flex justify-between gap-2">
          <span className="whitespace-nowrap">Uploading {active.length} {active.length === 1 ? "file" : "files"}</span>
          <span className="shrink-0 tabular-nums text-muted-foreground">{rate > 0 ? formatNetworkRate(rate) : `${percent}%`}</span>
        </div>
        <Progress value={percent} className="mt-1 h-1 motion-reduce:transition-none" aria-label="Upload progress" />
      </div>
    </div>
  );
}
