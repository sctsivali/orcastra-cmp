import * as React from "react";
import { type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { TableCell, TableRow } from "@/components/ui/table";

// The single placeholder for a value the API did not return. Six spellings existed before
// this ("—", "N/A", "-", "None", "No description", "No groups"). The em dash wins because
// formatLabel() in lib/utils.ts already returns it for empty input, so any other choice
// would put the helper and the convention in conflict.
export const EMPTY_VALUE = "—";

// important: the label replaces the glyph for assistive tech. A screen reader announcing
// "em dash" tells the user nothing about why the cell is blank.
export function TableEmptyValue({ label = "No value" }: { label?: string }) {
  return (
    <span className="text-muted-foreground" aria-label={label}>
      {EMPTY_VALUE}
    </span>
  );
}

// The only sanctioned second text size in a table: a subordinate qualifier rendered under
// the identifier value (a cluster id under a display name, host/project under an instance
// name). Never use it as a standalone cell value.
export function TableSubText({
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn("block truncate text-xs text-muted-foreground", className)}
      {...props}
    />
  );
}

// The one "no rows" row. Replaces the py-6 / py-8 / py-10 / no-padding variants and the
// pages that rendered their empty state as a sibling div outside the table entirely.
// note: p-0 on the cell because EmptyState brings its own py-12.
export function TableEmptyRow({
  colSpan,
  icon,
  iconClassName,
  title,
  description,
  action,
}: {
  colSpan: number;
  icon?: LucideIcon;
  /** ex; "animate-spin" to reuse the same row shape for a loading placeholder. */
  iconClassName?: string;
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <TableRow className="hover:bg-transparent">
      <TableCell colSpan={colSpan} className="p-0">
        <EmptyState
          icon={icon}
          iconClassName={iconClassName}
          title={title}
          description={description}
          action={action}
        />
      </TableCell>
    </TableRow>
  );
}
