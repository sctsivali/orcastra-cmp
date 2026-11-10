"use client";

import { Clock, Lock } from "lucide-react";
import type { TenantAccessSummary, TenantTermItem } from "@/lib/access-api";
import { cn, formatDateTime, plural } from "@/lib/utils";

// Shows the paid term behind a Tenant's access (backend core/tenant_term.py) on every page: after
// the term, instances are read-only; after the retention window, the project is deleted. Uses the
// access summary the shell already fetched.

const SHOWN = 3;

function line(item: TenantTermItem): string {
  const project = item.project_name;
  if (item.phase === "ending") {
    return `Subscription for ${project} ends ${formatDateTime(item.term_ended_at ?? item.access_ends_at)}. After that its instances become read-only.`;
  }
  if (item.inferred || !item.term_ended_at) {
    return `Subscription for ${project} ended. Its instances are read-only until your provider removes them.`;
  }
  if (item.access_ends_at) {
    return `Subscription for ${project} ended ${formatDateTime(item.term_ended_at)}. Read-only access until ${formatDateTime(item.access_ends_at)}, then the project is deleted.`;
  }
  return `Subscription for ${project} ended ${formatDateTime(item.term_ended_at)}. Its instances are read-only until your provider removes them.`;
}

export function TenantTermNotice({ summary }: { summary: TenantAccessSummary | null }) {
  const items = summary?.items ?? [];
  if (items.length === 0) return null;
  const readOnly = items.some((item) => item.phase === "read_only");
  const Icon = readOnly ? Lock : Clock;
  const shown = items.slice(0, SHOWN);
  const more = items.length - shown.length;

  return (
    <div
      data-testid="tenant-term-notice"
      data-phase={readOnly ? "read_only" : "ending"}
      className={cn("border-b", readOnly ? "border-warning/40 bg-warning/10" : "bg-muted/50")}
    >
      <div role="status" aria-live="polite" className="container flex gap-3 px-4 py-3 text-sm md:px-6 lg:px-8">
        <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", readOnly ? "text-warning" : "text-muted-foreground")} aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium text-foreground">{readOnly ? "Read-only access" : "Subscription ending soon"}</p>
          {shown.map((item) => (
            <p key={`${item.cluster_id}/${item.project_name}`} className="text-muted-foreground break-words">
              {line(item)}
            </p>
          ))}
          {more > 0 && <p className="text-muted-foreground">and {plural(more, "more project")}.</p>}
          {readOnly && (
            <p className="text-muted-foreground">To keep using them, renew the subscription with your provider.</p>
          )}
        </div>
      </div>
    </div>
  );
}
