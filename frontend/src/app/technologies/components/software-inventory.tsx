"use client";

import * as React from "react";
import { Server } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { EmptyClustersIllustration } from "@/components/illustrations/scenes/onboarding";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import type { NodeStatus } from "@/types/api";
import { fleetDivergence } from "./software-fields";
import { SoftwareNodeCard } from "./software-node-card";

/**
 * The software inventory, one card per connected node.
 *
 * No single node speaks for the fleet: nodes can differ in OS, kernel and architecture.
 *
 * The section opens the way both hardware sections do, with an h2 heading and a one-line
 * subtitle, so the heading order runs from the page h1 to the h3 inside each node card.
 */
export function SoftwareInventory({ nodes }: { nodes: NodeStatus[] }) {
  const connected = nodes.filter((node) => node.connected && node.environment);
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(GRID_PAGE_SIZE);
  const unreachable = nodes.filter((node) => !node.connected);
  const divergence = fleetDivergence(connected);

  // Clamped on read. Each card is expanded by default with two detail grids, a runtimes grid
  // and a storage grid, so one card is roughly a screenful.
  //
  // `divergence` covers every connected node, not the page: a fleet that disagrees about a
  // version says so on every page.
  const totalPages = Math.max(1, Math.ceil(connected.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedNodes = connected.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // important: `name` is the unique node id, `display_name` a cosmetic label two nodes can
  // share. The banner keys its reasons by the label it is handed, so a shared label would
  // attribute one node's failure to the other. Fall back to the id when the label is not unique.
  const labelFor = (node: NodeStatus) => {
    const label = node.display_name || node.name;
    const shared = unreachable.filter((other) => (other.display_name || other.name) === label);
    return shared.length > 1 ? node.name : label;
  };
  const unreachableNames = unreachable.map(labelFor);
  const unreachableReasons = Object.fromEntries(
    unreachable.map((node) => [labelFor(node), node.error ?? ""]),
  );

  // With no cluster connected, the subtitle describes the section instead of repeating the
  // empty state directly under it.
  const subtitle =
    connected.length === 0
      ? "Each cluster reports its own software inventory, and they do not have to agree."
      : connected.length === 1
        ? "Reported by the one connected cluster. A missing version means the cluster does not report that component, not that it is absent."
        : `Reported separately by each of the ${connected.length} connected clusters, because they do not have to agree. A missing version means the cluster does not report that component, not that it is absent.`;

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-xl font-semibold">Software by Cluster</h2>
        {/* --foreground on the page background, same reason as the hardware sections. */}
        <p className="text-sm text-foreground">{subtitle}</p>
        {divergence.length > 0 ? (
          <p className="text-sm text-foreground mt-1">
            This fleet is running {divergence.join(", ")}.
          </p>
        ) : null}
      </div>

      {/* important: outside the connected branch, so a fleet that is entirely down still names
          which nodes are silent and why. */}
      <UnreachableNodesBanner
        nodes={unreachableNames}
        reasons={unreachableReasons}
        noun="software inventories"
      />

      {connected.length === 0 ? (
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              icon={Server}
              illustration={EmptyClustersIllustration}
              title="No cluster reports a software inventory yet"
              description="Versions appear here once a cluster is connected and answering."
            />
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-6">
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-6 items-start">
            {pagedNodes.map((node) => (
              <SoftwareNodeCard key={node.name} node={node} />
            ))}
          </div>

          {/* Absent rather than disabled when every connected node fits on one page. */}
          {totalPages > 1 && (
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              totalItems={connected.length}
              pageSize={pageSize}
              pageSizeOptions={[...GRID_PAGE_SIZES]}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          )}
        </div>
      )}
    </section>
  );
}
