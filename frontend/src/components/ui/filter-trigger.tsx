"use client";

import * as React from "react";
import { ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";

export interface FilterTriggerProps {
  label: string;
  /** The active selection, shown in a badge. Absent when the filter is inactive. */
  summary?: string;
  /** Count badge for a multi-select. Takes precedence over `summary`. */
  count?: number;
  open: boolean;
  controls: string;
  field: string;
  kind: string;
  disabled?: boolean;
}

/**
 * The one toolbar trigger, shared by every filter control.
 *
 * note: extracted rather than copied. Four independently maintained triggers is exactly how
 * this codebase ended up with four divergent search inputs, and the same drift was starting
 * again the moment a second control needed the same button.
 *
 * The data attributes exist for the browser tests: targeting a control by field and kind is
 * stable, where matching a label prefix goes ambiguous as soon as two controls on one page
 * share one.
 */
export const FilterTrigger = React.forwardRef<HTMLButtonElement, FilterTriggerProps>(
  function FilterTrigger(
    { label, summary, count, open, controls, field, kind, disabled, ...rest },
    ref,
  ) {
    const active = count !== undefined ? count > 0 : Boolean(summary);
    return (
      <button
        // important: `rest` is spread FIRST so this component's own attributes win. Radix
        // PopoverTrigger injects its props through asChild, including an aria-controls pointing
        // at the id it generated for the panel. Every control here labels its panel itself, so
        // with the spread last that injected value silently replaced ours and referenced an id
        // no element carried. Radix's own onClick, data-state and aria-haspopup still arrive
        // through the spread; nothing below re-declares them.
        {...rest}
        ref={ref}
        type="button"
        aria-expanded={open}
        aria-controls={controls}
        disabled={disabled}
        data-filter-field={field}
        data-filter-kind={kind}
        className="flex h-9 max-w-[22rem] items-center gap-2 rounded-md border border-input bg-background px-3 text-sm ring-offset-background hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {/* The weight carries the active state alongside the badge. --primary is the
            user-configurable accent and is not contrast-clamped, so it is never the only
            signal. */}
        <span className={cn("shrink-0", active && "font-medium")}>{label}</span>
        {count !== undefined && count > 0 && (
          <Badge variant="secondary" className="px-1.5 py-0 text-xs tabular-nums">
            {count}
          </Badge>
        )}
        {count === undefined && summary && (
          <Badge variant="secondary" className="min-w-0 px-1.5 py-0 text-xs font-normal">
            <span className="truncate">{summary}</span>
          </Badge>
        )}
        <ChevronDown aria-hidden="true" className="h-4 w-4 shrink-0 opacity-50" />
      </button>
    );
  },
);
