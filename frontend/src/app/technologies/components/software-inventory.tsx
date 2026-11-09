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
 * This replaces a single set of cards built from connectedNodes[0]. On a fleet of Ubuntu 24.04
 * on x86_64 and Ubuntu 22.04 on aarch64 that read as "the platform runs 24.04", which is the
 * same class of untrue statement the hardcoded values on this page used to make: one node
 * quietly speaking for all of them.
 *
 * The section opens the way both hardware sections do, with a heading and a one-line subtitle
 * rather than a bordered callout. The callout was the only one on the page, so the two tabs
 * read as different products, and it carried no heading, which left this tab jumping from the
 * page h1 straight to the h3 inside each node card.
 */
export function SoftwareInventory({ nodes }: { nodes: NodeStatus[] }) {
  const connected = nodes.filter((node) => node.connected && node.environment);
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(GRID_PAGE_SIZE);
  const unreachable = nodes.filter((node) => !node.connected);
  const divergence = fleetDivergence(connected);

  // Clamped on read. These cards are heavier than the hardware tab's: each is expanded by
  // default with two detail grids, a runtimes grid and a storage grid, so one card is roughly a
  // screenful and the page length tracked the fleet exactly.
  //
  // note: `divergence` is computed over every connected node, not the page, because a fleet
  // that disagrees about a version has to say so whichever page you are on.
  const totalPages = Math.max(1, Math.ceil(connected.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedNodes = connected.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // important: `name` is the unique node id, `display_name` is a cosmetic label two nodes can
  // share (types/api.ts marks it optional). The banner keys its reasons by the label it is
  // handed, so a shared label would attribute one node's failure to the other. Fall back to the
  // id whenever the friendly label is not unique within this list.
  const labelFor = (node: NodeStatus) => {
    const label = node.display_name || node.name;
    const shared = unreachable.filter((other) => (other.display_name || other.name) === label);
    return shared.length > 1 ? node.name : label;
  };
  const unreachableNames = unreachable.map(labelFor);
  const unreachableReasons = Object.fromEntries(
    unreachable.map((node) => [labelFor(node), node.error ?? ""]),
  );

  // note: the zero-connected line describes the section rather than repeating the empty state
  // sitting directly under it. The other two are the copy this section has always carried.
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
          which nodes are silent and why. It used to sit below the node grid, which the
          all-unreachable case never reached, and that is the ordinary state for a partner. */}
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
