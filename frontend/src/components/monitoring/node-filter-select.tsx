"use client";

import { useMemo } from "react";
import { Server } from "lucide-react";

import { SearchableSelect } from "@/components/ui/searchable-select";
import type { SearchableSelectOption } from "@/components/ui/searchable-select";
import { clusterStatusMeta } from "@/lib/cluster-status";
import type { ClusterMonitoringMetrics } from "@/types/api";

export const ALL_NODES = "all";

interface NodeFilterSelectProps {
  nodes: ClusterMonitoringMetrics[];
  value: string;
  onValueChange: (value: string) => void;
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
}: NodeFilterSelectProps) {
  const options = useMemo<SearchableSelectOption[]>(() => {
    const rows = nodes.map((node) => {
      const meta = clusterStatusMeta(node);
      return {
        value: node.node_name,
        label: node.node_name,
        status: {
          label: meta.label,
          dotClassName: meta.dotClassName,
          tone: meta.tone,
        },
        _rank: meta.toneRank,
      };
    });

    // Reachable nodes lead. With two answering out of eight, alphabetical order buries both.
    rows.sort((a, b) => a._rank - b._rank || a.label.localeCompare(b.label));

    return [
      { value: ALL_NODES, label: "All Nodes" },
      ...rows.map(({ _rank, ...opt }) => opt),
    ];
  }, [nodes]);

  return (
    <SearchableSelect
      value={value}
      onValueChange={onValueChange}
      options={options}
      icon={Server}
      ariaLabel="Filter by node"
      placeholder="All Nodes"
      searchPlaceholder="Search nodes..."
      emptyText="No nodes found."
      // The trigger sits in a toolbar beside the pause and refresh buttons, so it keeps its own
      // width and the 36px height those share rather than the form-field default.
      className="h-9 w-auto min-w-[160px] max-w-[300px]"
      // The panel inherits the trigger width, which is too narrow once a row carries a name and
      // a status word. Widened, and still capped to what the viewport can give it.
      contentClassName="w-[min(20rem,var(--radix-popover-content-available-width))]"
    />
  );
}
