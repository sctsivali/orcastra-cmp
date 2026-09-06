"use client";

import * as React from "react";
import type { NodeStatus } from "@/types/api";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import { NodeHardwareCard } from "./node-hardware-card";

/**
 * Per-node hardware, under the fleet totals.
 *
 * Reachable nodes first, then the ones that are not answering: a disabled card that names its
 * own reason is more useful than omitting the node, because an operator looking for a machine
 * needs to see that it is registered and silent rather than absent.
 */
export function NodeHardwareList({ nodes }: { nodes: NodeStatus[] }) {
  const ordered = React.useMemo(() => {
    const reporting = nodes.filter((node) => node.connected && node.resources?.hardware?.detected);
    const rest = nodes.filter((node) => !reporting.includes(node));
    return [...reporting, ...rest];
  }, [nodes]);

  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(GRID_PAGE_SIZE);

  // Clamped on read. One card per registered cluster, and nothing is ever removed from the list,
  // so this is the same collection the overview grid already pages at the same size.
  const totalPages = Math.max(1, Math.ceil(ordered.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedNodes = ordered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (ordered.length === 0) return null;

  const reportingCount = ordered.filter(
    (node) => node.connected && node.resources?.hardware?.detected,
  ).length;

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-xl font-semibold">Hardware by Node</h2>
        {/* --foreground on the page background, same reason as the aggregate section. */}
        <p className="text-sm text-foreground">
          {reportingCount === 0
            ? "No node is currently reporting hardware detail."
            : `${reportingCount} of ${ordered.length} registered ${
                ordered.length === 1 ? "node reports" : "nodes report"
              } hardware detail. Expand a node to see what it detected.`}
        </p>
      </div>

      {/* important: the nodes are listed even when none of them reports anything. Replacing the
          list with an empty state hides the registered-but-silent nodes, and an operator looking
          for a machine needs to see that it is registered and quiet rather than gone. Found by
          the partner role, whose whole fleet is unreachable. */}
      <div className="space-y-3">
        {pagedNodes.map((node) => (
          <NodeHardwareCard key={node.name} node={node} />
        ))}
      </div>

      {/* Absent rather than disabled when the whole fleet fits on one page. No mt wrapper: the
          section is already a space-y-3 stack, whose gap beats a margin on the child. */}
      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          totalItems={ordered.length}
          pageSize={pageSize}
          pageSizeOptions={[...GRID_PAGE_SIZES]}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      )}
    </section>
  );
}
