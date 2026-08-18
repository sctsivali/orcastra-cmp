import * as React from "react";
import { cn } from "@/lib/utils";

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

const Table = React.forwardRef<
  HTMLTableElement,
  React.HTMLAttributes<HTMLTableElement> & { density?: TableDensity }
>(({ className, density = "default", ...props }, ref) => (
  <TableDensityContext.Provider value={density}>
    <div className="relative w-full overflow-auto">
      <table
        ref={ref}
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  </TableDensityContext.Provider>
));
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
