"use client";

import * as React from "react";
import { ArrowDownWideNarrow, ArrowUpNarrowWide } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import type { SortDirection } from "@/lib/sort";

export interface SortSelectOption<F extends string> {
  value: F;
  label: string;
}

export interface SortSelectProps<F extends string> {
  options: readonly SortSelectOption<F>[];
  /** The field currently sorted on. */
  value: F;
  direction: SortDirection;
  /**
   * Same contract as SortableTableHead: called with the field to sort by. Pass useTableSort's
   * handleSort so a card grid and a table agree on what selecting a dimension means.
   */
  onSort: (field: F) => void;
  /** Visible text naming the control. */
  label?: string;
  className?: string;
}

/**
 * The visible counterpart to SortableTableHead, for a list with no column headers to click.
 *
 * The direction is a separate button rather than two entries per field, so the options stay
 * scannable and the current order shows as the button's own state.
 *
 * A single option renders as text plus the same direction toggle, with no Select: a one-entry
 * Select can choose nothing, while a one-dimension grid still needs to show and reverse its order.
 */
export function SortSelect<F extends string>({
  options,
  value,
  direction,
  onSort,
  label = "Sort by",
  className,
}: SortSelectProps<F>) {
  const labelId = React.useId();
  const activeLabel = options.find((option) => option.value === value)?.label ?? value;
  const Icon = direction === "asc" ? ArrowUpNarrowWide : ArrowDownWideNarrow;

  // The name states both the order in force and the order a click produces, so a screen reader
  // can tell which way the list runs. check-icon-button-names.mjs only catches unnamed buttons.
  // An arrow rather than a chevron, because chevrons mark disclosure controls in this app.
  // Hoisted so both branches below render the identical control with the identical name.
  const directionButton = (
    <Button
      variant="outline"
      size="sm"
      className="h-9 w-9 p-0 shrink-0"
      onClick={() => onSort(value)}
      aria-label={
        direction === "asc"
          ? `Sorted by ${activeLabel}, ascending. Sort descending`
          : `Sorted by ${activeLabel}, descending. Sort ascending`
      }
    >
      <Icon className="h-4 w-4" aria-hidden="true" />
    </Button>
  );

  if (options.length === 1) {
    return (
      <div className={cn("flex items-center gap-2", className)}>
        <span className="whitespace-nowrap text-sm font-medium">
          {label} {activeLabel}
        </span>
        {directionButton}
      </div>
    );
  }

  return (
    <div className={cn("flex items-center gap-2", className)}>
      {/* A Radix trigger is a button rather than an input, so htmlFor would associate with
          nothing. aria-labelledby names it, as in range-filter.tsx and text-filter.tsx. */}
      <span id={labelId} className="whitespace-nowrap text-sm font-medium">
        {label}
      </span>

      <Select
        value={value}
        onValueChange={(next) => {
          // important: handleSort flips the direction when handed the field already selected
          // (use-table-sort.ts). Without this guard, re-picking the current entry would silently
          // reverse the whole grid.
          if (next !== value) onSort(next as F);
        }}
      >
        <SelectTrigger aria-labelledby={labelId} className="h-9 w-[150px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {directionButton}
    </div>
  );
}
