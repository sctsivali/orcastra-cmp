"use client";

import type { ReactNode } from "react";
import { ArrowUpDown, ChevronDown, ChevronUp } from "lucide-react";
import { TableHead, useTableDensity } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { SortDirection } from "@/lib/sort";

// Shared sortable column header. The active column shows a chevron for the current direction;
// inactive columns show a faint neutral indicator so an operator can see which headers respond.
//
// important: the control is a real <button> inside the th, not an onClick on the th itself. A th
// is not focusable, gets no focus ring and ignores Enter, so the previous shape made column
// sorting mouse-only in all 26 tables. It is the same defect check-table-typography.mjs already
// hard-bans for TableCell and TableRow, one element further out, and it survived because that
// rule lists those two element names and not this one. The rule now names all three.
//
// important: the height and horizontal padding move onto the button and off the th, so the
// button fills the cell. Left on the th they would leave a click target the size of the label
// with a hover band to match, which is a worse target than the one being replaced.
//
// note: aria-sort belongs on the column header cell, not on the button, which is why the th
// keeps it while the button carries the label.
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
