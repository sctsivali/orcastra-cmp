"use client";

import * as React from "react";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue } from "@/components/ui/table-text";
import { projectsFromUsedBy } from "@/lib/used-by";

// The two cells that open the shared dialog. They stay two, because the columns behind them
// carry different sort and facet accessors and different URL keys, and merging them would take
// out the `projects` facet and its role pin along with any shared link that uses it.
//
// note: neither cell owns a dialog. They set state on a controller that a single hoisted
// UsedByDialog reads, which is why a 25-row table now mounts one Radix root instead of fifty.

interface UsedByCellProps {
  usedBy: readonly string[] | null | undefined;
  /** Plural noun for the count, ex; "volumes". Matches what the column header promises. */
  noun: string;
  subjectName: string;
  emptyLabel: string;
  onOpen: () => void;
}

export function UsedByCell({ usedBy, noun, subjectName, emptyLabel, onOpen }: UsedByCellProps) {
  const count = usedBy?.length ?? 0;
  if (count === 0) return <TableEmptyValue label={emptyLabel} />;
  return (
    <TableLink onClick={onOpen} aria-label={`Show the ${count} ${noun} using ${subjectName}`}>
      {`${count} ${noun}`}
    </TableLink>
  );
}

interface ProjectsCellProps {
  usedBy: readonly string[] | null | undefined;
  subjectName: string;
  onOpen: () => void;
}

export function ProjectsCell({ usedBy, subjectName, onOpen }: ProjectsCellProps) {
  // Hits the same WeakMap entry the column's own facet accessor already warmed, so the count
  // costs no extra parsing.
  const count = projectsFromUsedBy(usedBy).length;
  if (count === 0) return <TableEmptyValue label="No projects" />;
  return (
    <TableLink onClick={onOpen} aria-label={`Show the ${count} project${count === 1 ? "" : "s"} using ${subjectName}`}>
      {`${count} project${count === 1 ? "" : "s"}`}
    </TableLink>
  );
}
