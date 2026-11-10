"use client";

import * as React from "react";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue } from "@/components/ui/table-text";
import { usePermissions } from "@/hooks/use-permissions";
import { inventoryLinks } from "@/lib/inventory-links";
import { cn } from "@/lib/utils";
import { Role } from "@/types/rbac";

// The one way a registered node id is rendered where an operator might want to follow it: a
// table cell, a sub-text qualifier, a chip, a detail field.
//
// important: the role gate lives here and nowhere else. Clusters is Partner-tier
// (components/sidebar/nav-config.tsx), so for a Tenant a link would be a dead end that lands on
// the welcome page. scripts/checks/check-node-link.mjs refuses any other caller of
// inventoryLinks.clusters.
//
// The value is the registered node id (`host`, `node`, `node_name`, `cluster_id`), which is what
// the Clusters page matches through CLUSTERS.searchExtra. Never a display name, and never an LXD
// cluster member name (`location`), which is a different namespace and stays plain text.
//
// No aria-label and no title. The accessible name must stay the visible id, which is how tests
// find the link, and a title is a hover-only reveal this codebase does not use.
export interface NodeLinkProps {
  /** The registered node id. Empty renders the shared placeholder. */
  clusterId: string | null | undefined;
  /** What to print instead of the id, when the surface already shows the id elsewhere. */
  label?: React.ReactNode;
  /** The placeholder's accessible name when the id is missing. */
  emptyLabel?: string;
  className?: string;
  /** Let a long id wrap instead of clip; see TableLink. */
  wrap?: boolean;
}

export function NodeLink({ clusterId, label, emptyLabel = "No cluster", className, wrap }: NodeLinkProps) {
  const { hasRole } = usePermissions();
  if (!clusterId) return <TableEmptyValue label={emptyLabel} />;
  const text = label ?? clusterId;
  if (hasRole(Role.PARTNER)) {
    return (
      <TableLink href={inventoryLinks.clusters({ q: clusterId })} className={className} wrap={wrap}>
        {text}
      </TableLink>
    );
  }
  return (
    <span data-node-link="text" className={cn("min-w-0", className)}>
      {text}
    </span>
  );
}
