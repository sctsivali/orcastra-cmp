import * as React from "react";
import { type LucideIcon } from "lucide-react";
import { cn, EMPTY_VALUE } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import type { IllustrationProps } from "@/components/illustrations/illustration-frame";
import { TableCell, TableRow } from "@/components/ui/table";

// The single placeholder for a value the API did not return: "—", the same glyph formatLabel()
// in lib/utils.ts returns for empty input.
export { EMPTY_VALUE };

// The label replaces the glyph for assistive tech. A screen reader announcing "em dash" tells
// the user nothing about why the cell is blank.
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
//
// `wrap` is for a qualifier that is a long opaque value with nowhere else to appear, ex; a
// cluster endpoint on a page that has no detail dialog. The clip stays the default; a caller
// that owns a reveal turns it off, or the reveal expands a box whose contents are still clipped.
export function TableSubText({
  className,
  wrap,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { wrap?: boolean }) {
  return (
    <span
      className={cn(
        "block text-xs text-muted-foreground",
        wrap ? "whitespace-normal break-words" : "truncate",
        className,
      )}
      {...props}
    />
  );
}

// The one "no rows" row. p-0 on the cell because EmptyState brings its own py-12.
export function TableEmptyRow({
  colSpan,
  icon,
  iconClassName,
  illustration,
  title,
  description,
  action,
}: {
  colSpan: number;
  icon?: LucideIcon;
  /** A first-run row only ("No X yet"), never a filter that matched nothing; see EmptyState. */
  illustration?: React.ComponentType<IllustrationProps>;
  /** ex; "animate-spin" to reuse the same row shape for a loading placeholder. */
  iconClassName?: string;
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <TableRow className="hover:bg-transparent">
      <TableCell colSpan={colSpan} className="p-0">
        {/* When the table scrolls sideways this cell spans every column, so the message sits in
            a box as wide as the visible area (measured by the Table container), pinned to the
            scroller's left edge. */}
        <div className="sticky left-0 max-w-[var(--table-visible-width,100%)]">
          <EmptyState
            icon={icon}
            iconClassName={iconClassName}
            illustration={illustration}
            title={title}
            description={description}
            action={action}
          />
        </div>
      </TableCell>
    </TableRow>
  );
}
