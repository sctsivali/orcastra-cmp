import * as React from "react";
import { Skeleton } from "@/components/ui/skeleton";

/** Compact-row height plus its border, so the placeholder holds the room the rows will take. */
const ROW_PX = 32;
/** Toolbar plus sticky header, the chrome above the first row. */
const CHROME_PX = 88;

export function skeletonRowsFor(height: number): number {
  return Math.max(2, Math.floor((height - CHROME_PX) / ROW_PX));
}

/**
 * Rows-shaped placeholder for a tab body that has not loaded yet. Sized to the dock height, so
 * the table that replaces it lands at the same height rather than growing under the operator.
 */
export function PanelSkeleton({ rows }: { rows: number }) {
  return (
    // note: no role="status" here. The panel owns the one polite live region on this surface,
    // and it announces the load itself; a second region would speak over it.
    <div className="flex flex-col px-3 py-2" aria-busy="true">
      <span className="sr-only">Loading</span>
      <Skeleton className="mb-2 h-8 w-64" />
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex h-8 items-center">
          <Skeleton className="h-6 w-full" />
        </div>
      ))}
    </div>
  );
}
