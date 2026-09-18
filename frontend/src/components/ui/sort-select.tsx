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
   * Same contract as SortableTableHead: called with the field to sort by. useTableSort's
   * handleSort is the intended argument, so a card grid and a table cannot end up disagreeing
   * about what selecting a dimension means.
   */
  onSort: (field: F) => void;
  /** Visible text naming the control. */
  label?: string;
  className?: string;
}

/**
 * The visible counterpart to SortableTableHead, for a list with no column headers to click.
 *
 * important: the direction is a separate button rather than two entries per field. Six options
 * for three dimensions stops being scannable, and it would leave "which way is this sorted right
 * now" to be read out of the selected option's text instead of off a control showing its own
 * state.
 *
 * note: a single option renders as text plus the same direction toggle, no Select. A Select
 * with one entry is a control that can choose nothing (the rule use-table-filters.ts already
 * states for facets), while a one-dimension grid still needs to say its order and reverse it.
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

  // important: the name states both the order in force and the order a click produces.
  // check-icon-button-names.mjs counts buttons with no name, never buttons whose name is
  // ambiguous, so a toggle labelled the same in both states passes the guard and still
  // leaves a screen reader unable to tell which way the list runs.
  // note: an arrow rather than a chevron. Chevrons are how this app draws disclosure, and
  // /inventory already carries two of them above the grid, so a third would make "the
  // expander" unaddressable for anything selecting on the icon.
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
      {/* note: a Radix trigger is a button rather than an input, so htmlFor would associate with
          nothing. aria-labelledby is what names it, the same shape range-filter.tsx and
          text-filter.tsx already use for their own panels. */}
      <span id={labelId} className="whitespace-nowrap text-sm font-medium">
        {label}
      </span>

      <Select
        value={value}
        onValueChange={(next) => {
          // important: handleSort flips the direction when it is handed the field already
          // selected (use-table-sort.ts), which is what makes a table header toggle. Re-picking
          // the current entry in a list is not that gesture, so without this guard reopening the
          // menu and choosing what is already chosen would silently reverse the whole grid.
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
