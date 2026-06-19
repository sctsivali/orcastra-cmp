"use client";

import type { ReactNode } from "react";
import { ArrowUpDown, ChevronDown, ChevronUp } from "lucide-react";
import { TableHead } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { SortDirection } from "@/lib/sort";

// Shared sortable column header. Active column shows a chevron for the current
// direction; inactive columns show a faint neutral indicator so users can see
// which headers are clickable.
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
  const isActive = currentField === field;
  return (
    <TableHead
      className={cn("cursor-pointer select-none hover:bg-muted/50", className)}
      onClick={() => onSort(field)}
    >
      <div className="flex items-center gap-1">
        {label}
        {isActive ? (
          direction === "asc" ? (
            <ChevronUp className="h-3.5 w-3.5" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" />
          )
        ) : (
          <ArrowUpDown className="h-3.5 w-3.5 opacity-30" />
        )}
      </div>
    </TableHead>
  );
}
