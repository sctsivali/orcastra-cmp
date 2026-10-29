"use client";

import { useMemo } from "react";
import { Server } from "lucide-react";

import { SearchableSelect } from "@/components/ui/searchable-select";
import type { SearchableSelectOption } from "@/components/ui/searchable-select";
import { usePermissions } from "@/hooks/use-permissions";
import { clusterStatusMeta, clusterSublabel } from "@/lib/cluster-status";
import { ClusterStatus, type ClusterMonitoringMetrics } from "@/types/api";

export const ALL_NODES = "all";

interface NodeFilterSelectProps {
  nodes: ClusterMonitoringMetrics[];
  value: string;
  onValueChange: (value: string) => void;
  /** The cluster's display name, from useClusterLabel. Defaults to the id. */
  labelOf?: (nodeName: string) => string;
}

/**
 * Which node the monitoring page is scoped to, and whether that node is actually answering.
 *
 * This was a plain Select over node names, so the page offered eight identical rows directly
 * above its own "Nodes Online 2 / 8" tile. The status was already in the payload these options
 * are built from; only the dropdown ignored it.
 *
 * A scope picker, not a target picker: nothing is ever disabled here. An operator opens
 * monitoring for the broken node far more often than for the healthy one, so removing it from
 * reach would defeat the page. Same reasoning as the policy scope pickers.
 */
export function NodeFilterSelect({
  nodes,
  value,
  onValueChange,
  labelOf = (nodeName) => nodeName,
}: NodeFilterSelectProps) {
  const { isPartner } = usePermissions();
  const options = useMemo<SearchableSelectOption[]>(() => {
    const rows = nodes.map((node) => {
      const raw = clusterStatusMeta(node);
      // important: "Pending Trust" and "Error" are certificate-handshake and platform
      // vocabulary a tenant cannot act on; the node card beside this filter already collapses
      // every not-online state to "Unavailable" for them, one word in one colour, so the list
      // must too.
      const meta =
        isPartner || raw.reachable
          ? raw
          : { ...clusterStatusMeta(ClusterStatus.OFFLINE), label: "Unavailable" };
      // The display name leads and the id sits under it, as on the Clusters page: the id is what
      // the URL and every table carry, so it has to stay findable.
      const label = labelOf(node.node_name);
      return {
        value: node.node_name,
        label,
        sublabel: clusterSublabel(node.node_name, label, undefined),
        status: {
          label: meta.label,
          tone: meta.tone,
        },
        _rank: meta.toneRank,
      };
    });

    // Reachable nodes lead. With two answering out of eight, alphabetical order buries both.
    rows.sort((a, b) => a._rank - b._rank || a.label.localeCompare(b.label));

    return [
      { value: ALL_NODES, label: "All Clusters" },
      ...rows.map(({ _rank, ...opt }) => opt),
    ];
  }, [nodes, isPartner, labelOf]);

  return (
    <SearchableSelect
      value={value}
      onValueChange={onValueChange}
      options={options}
      icon={Server}
      ariaLabel="Filter by cluster"
      placeholder="All Clusters"
      searchPlaceholder="Search clusters..."
      emptyText="No clusters found."
      // The trigger sits in a toolbar beside the pause and refresh buttons, so it keeps its own
      // width and the 36px height those share rather than the form-field default.
      className="h-9 w-auto min-w-[160px] max-w-[300px]"
      // The panel inherits the trigger width, which is too narrow once a row carries a name and
      // a status word. Widened, and still capped to what the viewport can give it.
      contentClassName="w-[min(20rem,var(--radix-popover-content-available-width))]"
    />
  );
}
