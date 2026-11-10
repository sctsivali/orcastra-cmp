"use client";

import { useState } from "react";
import { ArrowUpCircle, ExternalLink, ShieldAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useReleaseStatus } from "@/hooks/use-release-status";
import { canDismiss, dismiss, isDismissed } from "@/lib/release-dismissal";
import { cn } from "@/lib/utils";
import { releaseNoticeCopy } from "./release-copy";

/**
 * Tells an Administrator, on every page, that the running version is out of date: end of life or
 * missing a security fix (cannot be dismissed), deprecated (back after seven days), or a newer
 * release (hidden until the next one). Other roles see nothing.
 */
export function ReleaseNotice() {
  const { data, scopeKey, isAdmin } = useReleaseStatus();
  const [, setDismissedAt] = useState(0);
  if (!isAdmin || !data) return null;
  const copy = releaseNoticeCopy(data);
  if (!copy || isDismissed(data, scopeKey)) return null;
  const critical = data.level === "critical";
  const Icon = critical ? ShieldAlert : ArrowUpCircle;

  return (
    <div
      className={cn(
        "border-b",
        critical ? "border-destructive/40 bg-destructive/10" : data.level === "warning" ? "border-warning/40 bg-warning/10" : "bg-muted/40",
      )}
      data-testid="release-notice"
      data-level={data.level}
    >
      <div
        role="status"
        aria-live="polite"
        className="container flex flex-col gap-3 px-4 py-3 text-sm sm:flex-row sm:items-start sm:justify-between md:px-6 lg:px-8"
      >
        <div className="flex min-w-0 gap-3">
          <Icon
            className={cn("mt-0.5 h-4 w-4 shrink-0", critical ? "text-destructive" : data.level === "warning" ? "text-warning" : "text-muted-foreground")}
            aria-hidden="true"
          />
          <div className="min-w-0 space-y-1">
            <p className="font-medium text-foreground">{copy.title}</p>
            <p className="text-muted-foreground">{copy.body}</p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 self-start">
          {data.upgrade_url && (
            <Button size="sm" variant={critical ? "default" : "outline"} asChild>
              <a href={data.upgrade_url} target="_blank" rel="noopener noreferrer">
                How to Upgrade
                <ExternalLink className="ml-2 h-3.5 w-3.5" aria-hidden="true" />
                <span className="sr-only"> (opens in a new tab)</span>
              </a>
            </Button>
          )}
          {data.notes_url && (
            <Button size="sm" variant="ghost" asChild>
              <a href={data.notes_url} target="_blank" rel="noopener noreferrer">
                Release Notes
                <span className="sr-only"> (opens in a new tab)</span>
              </a>
            </Button>
          )}
          {canDismiss(data) && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                dismiss(data, scopeKey);
                setDismissedAt(Date.now());
              }}
            >
              {data.level === "warning" ? "Remind Me in 7 Days" : "Dismiss"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
