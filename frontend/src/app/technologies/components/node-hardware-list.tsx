"use client";

import * as React from "react";
import type { NodeStatus } from "@/types/api";
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

  if (ordered.length === 0) return null;

  const reportingCount = ordered.filter(
    (node) => node.connected && node.resources?.hardware?.detected,
  ).length;

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-xl font-semibold">Hardware by node</h2>
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
        {ordered.map((node) => (
          <NodeHardwareCard key={node.name} node={node} />
        ))}
      </div>
    </section>
  );
}
