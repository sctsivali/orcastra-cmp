import * as React from "react";
import { cn } from "@/lib/utils";

// A single pixel of slack, for the same sub-pixel rounding reason as lib/overflow-observer.ts.
const EDGE_SLACK = 1;

type ScrollX = "none" | "start" | "middle" | "end";

// Where the container currently sits on its horizontal scroll range, so the shadow can mark the
// edge that still has content behind it, and whether it scrolls at all, so only a container that
// does takes a tab stop.
//
// note: local and unexported rather than lib/overflow-observer.ts. That module reports only when
// the clipped state CHANGES, which is right for a reveal control and wrong here: an edge shadow
// needs a value on every scroll, and the edges have to be recomputed on a resize that does not
// flip the overflow state at all. Two different contracts, so they stay two mechanisms rather
// than one broadened past what its own docblock allows. Unexported so no screen can reach for it
// and start inventing its own scroll treatment.
//
// The two consumers want opposite defaults before the first measurement, so this returns both.
//
// important: `scrollable` starts TRUE. The tab stop hangs off it, and a control that appears only
// once JavaScript has decided it is needed is missing in exactly the window where it is needed.
// Same reasoning as the pessimistic default in lib/overflow-observer.ts. An unnecessary tab stop
// for one frame costs nothing; a missing one strands a keyboard user.
//
// important: `scrollX` starts "none". The shadow hangs off it, and the server renders this
// attribute into the HTML, which the browser paints before hydration. Starting it at "start"
// drew a shadow on every table on the page, including the ones that fit, until the effect ran.
// A hint that says there are more columns when there are none is worse than a hint that arrives
// a frame late, which is the opposite trade to the tab stop above.
function useHorizontalScrollState(ref: React.RefObject<HTMLDivElement>): {
  scrollX: ScrollX;
  scrollable: boolean;
} {
  const [state, setState] = React.useState<ScrollX>("none");
  const [measured, setMeasured] = React.useState(false);

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;

    // note: setState with an unchanged value does not re-render, so a scroll that stays inside
    // one bucket costs a read and nothing else. Only crossing an edge re-renders.
    const read = () => {
      setMeasured(true);
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

// Tailwind preflight sets `border-collapse: collapse`, and a collapsed border belongs to the table
// grid rather than to the cell it was written on. So a sticky `th` leaves its `border-b` behind and
// the pinned header ends up with no separator while a line slides away underneath it. Draw the
// separator as an inset shadow on the `th`, which travels with the sticky cell, and drop the row
// border so there is never a doubled line.
//
// note: `bg-background` because a sticky header needs an opaque ground or the rows read straight
// through it, and every surface this is used on is a dialog, whose ground is --background.
// --card is a different colour in both themes, so a table pinned on a card ground has to say so on
// its own TableHeader.
const STICKY_HEADER =
  "[&_thead_tr]:border-b-0 [&_thead_th]:sticky [&_thead_th]:top-0 [&_thead_th]:z-10 " +
  "[&_thead_th]:bg-background [&_thead_th]:shadow-[inset_0_-1px_0_hsl(var(--border))]";

// Two densities, and only two. `compact` exists because nested tables inside a dialog
// genuinely need shorter rows to fit a max-height, and the alternative that grew here
// organically was ~23 per-cell `text-xs px-3 py-1.5` overrides that opted individual cells
// out of the shared baseline. A named density keeps one typography system with one
// deliberate second step, instead of every call site inventing its own.
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
   * Classes for the scroll container, NOT for the table. Height, border and radius belong here.
   *
   * important: this element is the only scroll container a table gets, and that is the whole
   * point of the prop. Wrapping `<Table>` in a second `overflow-*` div splits the two axes
   * across two boxes: the outer one is the one with a bounded height, so it owns vertical
   * scroll, while the inner one stretches to the full height of the table and therefore paints
   * its horizontal scrollbar below the last row, off screen at every scroll position but the
   * very bottom. That shipped in the image picker and is what this prop exists to prevent.
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
   * note: optional, and the tab stop does not depend on it. An unnamed `role="region"` is not
   * exposed as a landmark at all, so a container that scrolls without a label takes focus (which
   * is what WCAG 2.1.1 is about) and declares no region, rather than declaring an anonymous one.
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
          // Read by the edge shadow in globals.css and by the Playwright geometry spec.
          data-scroll-x={scrollX}
          // important: a box an operator can only pan with a mouse drag or a shift-wheel is
          // unreachable from the keyboard, which is WCAG 2.1.1. A tab stop gives it the arrow
          // keys, Home and End. Added only when it actually scrolls, so a table that fits does
          // not cost a stop on the way to the next control.
          tabIndex={scrollable ? 0 : undefined}
          role={scrollable && scrollLabel ? "region" : undefined}
          aria-label={scrollable && scrollLabel ? scrollLabel : undefined}
          className={cn(
            "relative w-full overflow-auto",
            // important: an outline, not the house `ring-2`. Tailwind implements a ring AS a
            // box-shadow, and the edge shadow keyed off data-scroll-x in globals.css is also a
            // box-shadow on this same element, at equal specificity and declared after
            // `@tailwind utilities`. The shadow would win and the focus indicator would silently
            // not paint. An outline is a different property, so the two coexist.
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
      "border-b transition-colors hover:bg-muted/50 data-[state=selected]:bg-muted",
      className
    )}
    {...props}
  />
));
TableRow.displayName = "TableRow";

// note: exported so SortableTableHead can put the head's own height and padding on the BUTTON
// it renders, rather than on the th around it. A th is not focusable, so the click target has to
// be a real control, and it has to fill the cell or the hover band shrinks to the text.
export function useTableDensity(): TableDensity {
  return React.useContext(TableDensityContext);
}

const TableHead = React.forwardRef<
  HTMLTableCellElement,
  React.ThHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => {
  const density = React.useContext(TableDensityContext);
  return (
    <th
      ref={ref}
      className={cn(
        HEAD_DENSITY[density],
        "text-left align-middle font-medium text-muted-foreground [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  );
});
TableHead.displayName = "TableHead";

const TableCell = React.forwardRef<
  HTMLTableCellElement,
  React.TdHTMLAttributes<HTMLTableCellElement>
>(({ className, ...props }, ref) => {
  const density = React.useContext(TableDensityContext);
  // One typography baseline for every data cell. Stated here rather than repeated at
  // ~190 call sites, which is how the sizes, weights and colours drifted apart in the
  // first place. Call sites add only font-medium (identifier column), font-mono,
  // alignment, width and responsive-visibility classes.
  return (
    <td
      ref={ref}
      className={cn(
        CELL_DENSITY[density],
        "align-middle font-normal text-foreground [&:has([role=checkbox])]:pr-0",
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
