import * as React from "react";
import { cn } from "@/lib/utils";

// A single pixel of slack, for the same sub-pixel rounding reason as lib/overflow-observer.ts.
const EDGE_SLACK = 1;

type ScrollX = "none" | "start" | "middle" | "end";

// Where the container sits on its horizontal scroll range (for the edge shadow) and whether it
// scrolls at all (only a container that does takes a tab stop). Local rather than
// lib/overflow-observer.ts, which reports only when the clipped state changes; the shadow needs a
// value on every scroll and resize.
//
// important: `scrollable` starts true, so a keyboard user is never stranded before the first
// measurement. `scrollX` starts "none" because the server renders it and the browser paints it
// before hydration; "start" would shadow every table, including the ones that fit.
function useHorizontalScrollState(ref: React.RefObject<HTMLDivElement>): {
  scrollX: ScrollX;
  scrollable: boolean;
} {
  const [state, setState] = React.useState<ScrollX>("none");
  const [measured, setMeasured] = React.useState(false);

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;

    // setState with an unchanged value does not re-render, so a scroll that stays inside one
    // bucket costs a read and nothing else. Only crossing an edge re-renders.
    const read = () => {
      setMeasured(true);
      // The visible width, for a full-width empty row that must stay in view while the table
      // scrolls sideways (TableEmptyRow in table-text.tsx). A style write, so it costs no render.
      el.style.setProperty("--table-visible-width", `${el.clientWidth}px`);
      const max = el.scrollWidth - el.clientWidth;
      if (max <= EDGE_SLACK) {
        setState("none");
        return;
      }
      const left = el.scrollLeft;
      setState(left <= EDGE_SLACK ? "start" : left >= max - EDGE_SLACK ? "end" : "middle");
    };

    read();
    el.addEventListener("scroll", read, { passive: true });

    // The container resizes with the viewport and the table resizes when rows or values change,
    // so both are observed. Neither alone catches the other.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(read);
    observer?.observe(el);
    const table = el.firstElementChild;
    if (table) observer?.observe(table);

    return () => {
      el.removeEventListener("scroll", read);
      observer?.disconnect();
    };
  }, [ref]);

  return { scrollX: state, scrollable: !measured || state !== "none" };
}

// Tailwind preflight sets `border-collapse: collapse`, so a border belongs to the table grid rather
// than to its cell, and a sticky `th` leaves its `border-b` behind. The separator is drawn as an
// inset shadow on the `th`, which travels with the sticky cell, and the row border is dropped so
// the line is never doubled.
//
// `bg-background` because a sticky header needs an opaque ground, and every surface this is used
// on is a dialog, whose ground is --background. A table pinned on a --card ground sets that on
// its own TableHeader.
const STICKY_HEADER =
  "[&_thead_tr]:border-b-0 [&_thead_th]:sticky [&_thead_th]:top-0 [&_thead_th]:z-10 " +
  "[&_thead_th]:bg-background [&_thead_th]:shadow-[inset_0_-1px_0_hsl(var(--border))] " +
  // The pinned corner's ground paints over the th's inset separator, so it draws the line itself,
  // on the same pixel row as the rest of the header rather than one below it.
  "[&_thead_th[data-pinned]]:before:bottom-0";

// A column pinned to the trailing edge, so the row-actions column of a wide table is never behind
// a horizontal scroll.
//
// A sticky cell paints above the row, so it redraws what the row draws: `before` is the opaque
// ground (`--pinned-ground`, else `--card`) and the row separator; `after` carries the row tints
// and, from globals.css, the edge shadow. Only TableRow's own tints are mirrored, so a row with
// its own bg-* or hover:bg-* class needs a TableRow state instead.
//
// The ground reaches one pixel below the cell, because Chromium paints a collapsed row border in
// the next cell's top strip.
//
// important: the last body row stops at its box (keyed on tbody, so the header keeps its line).
// There is no next cell there, and the extra pixel would raise a 10 px vertical scrollbar.
const PINNED_END =
  "sticky end-0 z-[1] " +
  "before:pointer-events-none before:absolute before:inset-x-0 before:top-0 before:-bottom-px before:-z-[2] before:border-b before:border-border before:bg-[var(--pinned-ground,hsl(var(--card)))] " +
  "[tbody>tr:last-child>&]:before:bottom-0 [tbody>tr:last-child>&]:before:border-b-0 " +
  "after:pointer-events-none after:absolute after:inset-0 after:-z-[1] after:transition-colors motion-reduce:after:transition-none " +
  "[tr:hover>&]:after:bg-muted/50 [tr[data-state=selected]>&]:after:bg-muted [tr[data-state=pending]>&]:after:bg-muted/30 " +
  "[tr[data-row-focus=true]>&]:after:bg-primary/10 [tr[data-row-focus]>&]:after:duration-700";

type PinnedEdge = "end";

// Two densities. `compact` is for nested tables inside a dialog that need shorter rows to fit a
// max-height, so call sites do not override cell padding and size one by one.
type TableDensity = "default" | "compact";

const TableDensityContext = React.createContext<TableDensity>("default");

const CELL_DENSITY: Record<TableDensity, string> = {
  default: "p-4 text-sm",
  compact: "px-3 py-1.5 text-xs",
};

const HEAD_DENSITY: Record<TableDensity, string> = {
  default: "h-12 px-4",
  compact: "h-8 px-3 text-xs",
};

export interface TableProps extends React.HTMLAttributes<HTMLTableElement> {
  density?: TableDensity;
  /**
   * Classes for the scroll container: height, border and radius. The table takes `className`.
   *
   * important: this element is the only scroll container a table gets. Wrapping `<Table>` in a
   * second `overflow-*` div splits the axes: the outer box owns vertical scroll, and the inner
   * one paints its horizontal scrollbar below the last row, off screen until the very bottom.
   * `scripts/checks/check-table-scroll.mjs` enforces it.
   */
  containerClassName?: string;
  /**
   * Pin the header while the body scrolls under it. Only meaningful when `containerClassName`
   * bounds the height, because the container is what scrolls; on a table that scrolls with the
   * window it does nothing and should not be set.
   */
  stickyHeader?: boolean;
  /**
   * What the operator is scrolling, in their words, ex; "base image catalogue". Names the scroll
   * region for a screen reader.
   *
   * Optional; the tab stop does not depend on it. An unnamed `role="region"` is not a landmark,
   * so a scrolling container without a label takes focus (WCAG 2.1.1) and declares no region.
   */
  scrollLabel?: string;
}

const Table = React.forwardRef<HTMLTableElement, TableProps>(
  (
    { className, containerClassName, density = "default", stickyHeader, scrollLabel, ...props },
    ref,
  ) => {
    const containerRef = React.useRef<HTMLDivElement>(null);
    const { scrollX, scrollable } = useHorizontalScrollState(containerRef);

    return (
      <TableDensityContext.Provider value={density}>
        <div
          ref={containerRef}
          data-slot="table-container"
          // Read by the edge shadow in globals.css and by the browser specs in frontend/e2e.
          data-scroll-x={scrollX}
          // A box that pans only by mouse drag or shift-wheel is unreachable from the keyboard
          // (WCAG 2.1.1); a tab stop gives it the arrow keys, Home and End. Added only when it
          // scrolls, so a table that fits costs no stop.
          tabIndex={scrollable ? 0 : undefined}
          role={scrollable && scrollLabel ? "region" : undefined}
          aria-label={scrollable && scrollLabel ? scrollLabel : undefined}
          className={cn(
            // Only a call site that bounds the height gets vertical scroll, so a stray pixel can
            // never raise a scrollbar on a table that grows with its rows.
            "relative w-full",
            containerClassName ? "overflow-auto" : "overflow-x-auto overflow-y-hidden",
            stickyHeader && "scroll-pt-12",
            // important: focus uses an outline. Tailwind implements the house `ring-2` as a
            // box-shadow, and the edge shadow keyed off data-scroll-x in globals.css is a
            // box-shadow on this element too, declared later at equal specificity, so a ring would
            // silently not paint.
            scrollable &&
              "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-ring",
            containerClassName,
          )}
        >
          <table
            ref={ref}
            className={cn(
              "w-full caption-bottom text-sm",
              stickyHeader && STICKY_HEADER,
              className,
            )}
            {...props}
          />
        </div>
      </TableDensityContext.Provider>
    );
  },
);
Table.displayName = "Table";

const TableHeader = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <thead ref={ref} className={cn("[&_tr]:border-b", className)} {...props} />
));
TableHeader.displayName = "TableHeader";

const TableBody = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tbody
    ref={ref}
    className={cn("[&_tr:last-child]:border-0", className)}
    {...props}
  />
));
TableBody.displayName = "TableBody";

const TableFooter = React.forwardRef<
  HTMLTableSectionElement,
  React.HTMLAttributes<HTMLTableSectionElement>
>(({ className, ...props }, ref) => (
  <tfoot
    ref={ref}
    className={cn(
      "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
      className
    )}
    {...props}
  />
));
TableFooter.displayName = "TableFooter";

const TableRow = React.forwardRef<
  HTMLTableRowElement,
  React.HTMLAttributes<HTMLTableRowElement>
>(({ className, ...props }, ref) => (
  <tr
    ref={ref}
    className={cn(
      // `pending` is a placeholder row for a create still in flight; stated here, beside
      // selected, so a pinned cell can mirror it (PINNED_END) without the row naming a colour.
      "border-b transition-colors hover:bg-muted/50 data-[state=selected]:bg-muted data-[state=pending]:bg-muted/30",
      // The post-create mark (hooks/use-row-focus.ts). Attribute present, either value, keeps
      // the long duration so the fade-out runs at 700 ms instead of snapping to 150. No
      // keyframe and no ring: a box-shadow on a tr is unreliable across engines.
      "data-[row-focus=true]:bg-primary/10 data-[row-focus]:duration-700 motion-reduce:data-[row-focus]:transition-none",
      className
    )}
    {...props}
  />
));
TableRow.displayName = "TableRow";

// Exported so SortableTableHead can put the head's height and padding on its button rather than
// on the th, so the click target fills the cell.
export function useTableDensity(): TableDensity {
  return React.useContext(TableDensityContext);
}

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement> & { pinned?: PinnedEdge }
>(({ className, pinned, ...props }, ref) => {
  const density = React.useContext(TableDensityContext);
  return (
    <th
      ref={ref}
      data-pinned={pinned}
      className={cn(
        HEAD_DENSITY[density],
        "text-left align-middle font-medium text-muted-foreground [&:has([role=checkbox])]:pr-0",
        pinned && PINNED_END,
        className
      )}
      {...props}
    />
  );
});
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement> & { pinned?: PinnedEdge }
>(({ className, pinned, ...props }, ref) => {
  const density = React.useContext(TableDensityContext);
  // One typography baseline for every data cell. Call sites add only font-medium (identifier
  // column), font-mono, alignment, width and responsive-visibility classes.
  return (
    <td
      ref={ref}
      data-pinned={pinned}
      className={cn(
        CELL_DENSITY[density],
        "align-middle font-normal text-foreground [&:has([role=checkbox])]:pr-0",
        pinned && PINNED_END,
        className
      )}
      {...props}
    />
  );
});
TableCell.displayName = "TableCell";

const TableCaption = React.forwardRef<
  HTMLTableCaptionElement,
  React.HTMLAttributes<HTMLTableCaptionElement>
>(({ className, ...props }, ref) => (
  <caption
    ref={ref}
    className={cn("mt-4 text-sm text-muted-foreground", className)}
    {...props}
  />
));
TableCaption.displayName = "TableCaption";

export type { TableDensity };
export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
};
