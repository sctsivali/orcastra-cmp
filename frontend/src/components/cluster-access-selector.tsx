"use client";

import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { cn, formatHypervisorType } from "@/lib/utils";
import type { AvailableCluster } from "@/types";

// note: search box appears only past this count so short cluster lists stay uncluttered
const SEARCH_THRESHOLD = 6;

interface ClusterAccessSelectorProps {
  clusters: AvailableCluster[];
  selectedClusterIds: string[];
  onToggle: (clusterId: string) => void;
}

// Multi-select cluster picker used when scoping an API key. Selection is tracked by id, so a
// cluster that is selected and then filtered out of view stays selected; the counter below
// always reflects the full selection, not just what is visible.
export function ClusterAccessSelector({
  clusters,
  selectedClusterIds,
  onToggle,
}: ClusterAccessSelectorProps) {
  const [search, setSearch] = React.useState("");
  const safeClusterList = Array.isArray(clusters) ? clusters : [];

  const filtered = React.useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return safeClusterList;
    return safeClusterList.filter((cluster) => {
      if (!cluster) return false;
      const name = String(cluster.name || "").toLowerCase();
      const endpoint = String(cluster.endpoint || "").toLowerCase();
      const type = formatHypervisorType(cluster.server_type).toLowerCase();
      return (
        name.includes(query) ||
        endpoint.includes(query) ||
        type.includes(query)
      );
    });
  }, [safeClusterList, search]);

  const showSearch = safeClusterList.length > SEARCH_THRESHOLD;

  return (
    <div className="space-y-2">
      <Label>Cluster Access</Label>
      {showSearch && (
        <SearchInput
          value={search}
          onChange={setSearch}
          placeholder="Search by name, endpoint, or type"
          label="Search clusters"
        />
      )}
      <div className="h-48 overflow-y-auto rounded-md border p-3">
        {safeClusterList.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">
            No clusters available.
          </p>
        ) : filtered.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">
            No clusters match &quot;{search}&quot;.
          </p>
        ) : (
          <div className="space-y-2">
            {filtered.map((cluster) => {
              if (!cluster || typeof cluster.id !== "string") return null;
              const clusterId = String(cluster.id);
              const clusterName = String(cluster.name || "");
              const clusterEndpoint = String(cluster.endpoint || "");
              const clusterType = formatHypervisorType(cluster.server_type);
              const isSelected = selectedClusterIds.includes(clusterId);

              return (
                <label
                  key={clusterId}
                  htmlFor={`cluster-${clusterId}`}
                  className={cn(
                    "flex cursor-pointer items-center justify-between rounded-md p-2 transition-colors",
                    isSelected
                      ? "border border-primary bg-primary/10"
                      : "bg-muted/50 hover:bg-muted"
                  )}
                >
                  <div className="flex items-center gap-3">
                    <input
                      type="checkbox"
                      id={`cluster-${clusterId}`}
                      checked={isSelected}
                      onChange={() => onToggle(clusterId)}
                      className="h-4 w-4 rounded border-primary text-primary focus:ring-primary"
                    />
                    <div>
                      <p className="text-sm font-medium">{clusterName}</p>
                      <p className="font-mono text-xs text-muted-foreground">
                        {clusterEndpoint}
                      </p>
                    </div>
                  </div>
                  <Badge variant="outline" className="text-xs">
                    {clusterType}
                  </Badge>
                </label>
              );
            })}
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Selected: {selectedClusterIds.length} cluster(s)
      </p>
    </div>
  );
}
