"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Info, Unlock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { useRelativeTime } from "@/hooks/use-relative-time";
import { cn, formatDateTime } from "@/lib/utils";
import type { HostCheck } from "@/types/host-health";
import { HOST_CHECK_ICON, HOST_STATUS, NEEDS_ATTENTION } from "./status-display";

interface HostCheckRowProps {
  check: HostCheck;
  onUnseal?: () => void;
}

function factValue(value: HostCheck["facts"][number]["value"]): string {
  if (value === null || value === undefined || value === "") return "None";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

/** One dependency: its state, what it means, what to do, and the facts behind it. */
export function HostCheckRow({ check, onUnseal }: HostCheckRowProps) {
  const attention = NEEDS_ATTENTION.has(check.status);
  // A problem opens with its facts in view; a healthy row keeps the page short. A row that turns
  // bad while the page is open follows, until the operator opens or closes it themselves.
  const [open, setOpen] = useState(attention);
  const toggledRef = useRef(false);
  useEffect(() => {
    if (!toggledRef.current) setOpen(attention);
  }, [attention]);
  const display = HOST_STATUS[check.status];
  const Icon = HOST_CHECK_ICON[check.id];
  const sinceMs = check.since ? check.since * 1000 : null;
  const since = useRelativeTime(sinceMs);
  const headingId = `host-check-${check.id}`;

  return (
    <Card aria-labelledby={headingId} data-check={check.id} data-status={check.status}>
      <CardContent className="space-y-3 p-4 md:p-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 gap-3">
            <Icon className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2 id={headingId} className="text-base font-semibold leading-tight">
                  {check.label}
                </h2>
                <Badge variant={display.tone}>{display.label}</Badge>
                {sinceMs !== null && check.status !== "not_configured" && (
                  <span className="text-xs text-muted-foreground">
                    {"since "}
                    <time dateTime={new Date(sinceMs).toISOString()}>{formatDateTime(new Date(sinceMs).toISOString())}</time>
                    {` (${since})`}
                  </span>
                )}
              </div>
              <p className={cn("text-sm text-pretty", attention ? "text-foreground" : "text-muted-foreground")}>
                {check.summary}
              </p>
              {attention && check.remediation && (
                <p className="text-sm text-muted-foreground text-pretty">
                  <span className="font-medium text-foreground">What to do: </span>
                  {check.remediation}
                </p>
              )}
            </div>
          </div>
          {check.actions.includes("unseal_vault") && onUnseal && (
            <Button size="sm" className="shrink-0 self-start" onClick={onUnseal}>
              <Unlock className="mr-2 h-4 w-4" aria-hidden="true" />
              Unseal Vault
            </Button>
          )}
        </div>

        {check.advisories.length > 0 && (
          <ul className="space-y-1">
            {check.advisories.map((advisory) => (
              <li key={advisory} className="flex gap-2 text-sm text-muted-foreground">
                <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span className="text-pretty">{advisory}</span>
              </li>
            ))}
          </ul>
        )}

        {check.facts.length > 0 && (
          <Collapsible
            open={open}
            onOpenChange={(next) => {
              toggledRef.current = true;
              setOpen(next);
            }}
          >
            <CollapsibleTrigger asChild>
              <Button variant="ghost" size="sm" className="-ml-2 h-8 px-2 text-muted-foreground" aria-controls={`${headingId}-facts`}>
                <ChevronDown
                  className={cn("mr-1 h-4 w-4 transition-transform motion-reduce:transition-none", open && "rotate-180")}
                  aria-hidden="true"
                />
                {open ? "Hide Details" : "Show Details"}
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent id={`${headingId}-facts`}>
              <dl className="grid gap-x-6 gap-y-2 pt-2 text-sm sm:grid-cols-[minmax(10rem,auto)_1fr]">
                {check.facts.map((fact, index) => (
                  <div key={`${fact.label}-${index}`} className="contents">
                    <dt className="text-muted-foreground">{fact.label}</dt>
                    <dd className="min-w-0 break-words font-medium tabular-nums">{factValue(fact.value)}</dd>
                  </div>
                ))}
              </dl>
              {check.latency_ms !== null && (
                <p className="pt-2 text-xs text-muted-foreground">Checked in {check.latency_ms} ms.</p>
              )}
            </CollapsibleContent>
          </Collapsible>
        )}
      </CardContent>
    </Card>
  );
}
