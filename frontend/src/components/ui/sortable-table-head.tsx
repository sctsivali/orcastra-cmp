"use client";

import type { ReactNode } from "react";
import { ArrowUpDown, ChevronDown, ChevronUp } from "lucide-react";
import { TableHead, useTableDensity } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { SortDirection } from "@/lib/sort";

// Shared sortable column header. The active column shows a chevron for the current direction;
// inactive columns show a faint neutral indicator so an operator can see which headers respond.
//
// The control is a real <button> inside the th, because a th is not focusable, gets no focus
// ring and ignores Enter. check-table-typography.mjs bans onClick on TableHead for that reason.
//
// The height and horizontal padding sit on the button rather than the th, so the button fills
// the cell instead of leaving a click target the size of the label.
//
// aria-sort belongs on the column header cell, so the th keeps it while the button carries the
// label.
const HIT_AREA: Record<"default" | "compact", string> = {
  default: "h-12 px-4 text-sm",
  compact: "h-8 px-3 text-xs",
};

export function SortableTableHead<T extends string>({
  label,
  field,
  currentField,
  direction,
  onSort,
  className,
}: {
  label: ReactNode;
  field: T;
  currentField: T;
  direction: SortDirection;
  onSort: (field: T) => void;
  className?: string;
}) {
  const density = useTableDensity();
  const isActive = currentField === field;
  const Icon = isActive ? (direction === "asc" ? ChevronUp : ChevronDown) : ArrowUpDown;

  return (
    <TableHead
      className={cn("p-0", className)}
      aria-sort={isActive ? (direction === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        onClick={() => onSort(field)}
        className={cn(
          HIT_AREA[density],
          "flex w-full select-none items-center gap-1 text-left font-medium text-muted-foreground",
          "hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-inset focus-visible:ring-ring",
          "transition-colors motion-reduce:transition-none",
        )}
      >
        {label}
        <Icon className={cn("h-3.5 w-3.5 shrink-0", !isActive && "opacity-30")} aria-hidden="true" />
      </button>
    </TableHead>
  );
}
