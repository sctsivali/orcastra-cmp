"use client";

import * as React from "react";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";

export interface ActiveFilterChip {
  id: string;
  columnLabel: string;
  valueLabel: string;
}

export interface ActiveFilterChipsProps {
  chips: readonly ActiveFilterChip[];
  onRemove: (id: string) => void;
  onClearAll: () => void;
  className?: string;
}

/**
 * Every filter currently narrowing the table, each removable.
 *
 * important: a chip is rendered for an active filter even when its facet control is not on
 * the toolbar, so a deep link can never filter the view with no way to see or undo it.
 *
 * Badge belongs here: table conventions allow it in a filter toolbar and ban it from data
 * cells, and the typography guard only counts badges inside a table body.
 */
export function ActiveFilterChips({
  chips,
  onRemove,
  onClearAll,
  className,
}: ActiveFilterChipsProps) {
  if (chips.length === 0) return null;

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <ul aria-label="Active filters" className="flex flex-wrap items-center gap-2">
        {chips.map((chip) => (
          <li key={chip.id}>
            <Badge variant="secondary" className="gap-1 pl-2 pr-1 font-normal">
              <span className="text-muted-foreground">{chip.columnLabel}:</span>
              <span>{chip.valueLabel}</span>
              <button
                type="button"
                /* Both halves in the name: a control announced only as "alpha" tells a
                   screen reader user nothing about what it removes. */
                aria-label={`Remove filter ${chip.columnLabel} ${chip.valueLabel}`}
                onClick={() => onRemove(chip.id)}
                className="rounded p-0.5 hover:bg-background/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X aria-hidden="true" className="h-3 w-3" />
              </button>
            </Badge>
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={onClearAll}
        className="rounded-sm px-2 py-1 text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        Clear all
      </button>
    </div>
  );
}
