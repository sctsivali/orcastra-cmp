"use client";

import * as React from "react";
import { AlertTriangle, Info } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { clusterStatusMeta, type ClusterStatusMeta } from "@/lib/cluster-status";
import { cn, formatHypervisorType, plural } from "@/lib/utils";
import { ClusterStatus, type AvailableCluster } from "@/types";

// The search box appears only past this many clusters.
const SEARCH_THRESHOLD = 6;

interface ClusterAccessSelectorProps {
  clusters: AvailableCluster[];
  selectedClusterIds: string[];
  onToggle: (clusterId: string) => void;
  /**
   * The cluster list is being read again. A grant the list does not hold yet may be a cluster
   * registered a moment ago, so it is not called unknown until the read answers.
   */
  listChecking?: boolean;
}

/** One row, whether it came from the cluster list or from a grant with no cluster behind it. */
interface Row {
  id: string;
  name: string;
  endpoint: string;
  typeLabel: string;
  /** Undefined when reachability could not be established. Absent is not offline. */
  meta?: ClusterStatusMeta;
  /**
   * A grant naming a cluster the list does not contain, so the only thing this row can do is
   * be unticked.
   */
  orphan: boolean;
}

function toRow(cluster: AvailableCluster): Row {
  const id = String(cluster.id);
  return {
    id,
    name: String(cluster.name || id),
    endpoint: String(cluster.endpoint || ""),
    typeLabel: formatHypervisorType(cluster.server_type),
    // Keep undefined: clusterStatusMeta(undefined) resolves to Unknown, a claim about the
    // cluster, while a missing status must render as nothing.
    meta: cluster.status ? clusterStatusMeta(cluster.status) : undefined,
    orphan: false,
  };
}

function orphanRow(clusterId: string, checking: boolean): Row {
  return {
    id: clusterId,
    name: clusterId,
    endpoint: "",
    typeLabel: formatHypervisorType(null),
    // No status while the list is being read: the grant may name a cluster registered a moment
    // ago, which "Unknown" would misdescribe.
    meta: checking ? undefined : clusterStatusMeta(undefined),
    orphan: true,
  };
}

/**
 * Multi-select cluster picker used when scoping an API key.
 *
 * Selection is tracked by id, so a cluster filtered out of view stays selected and the counter
 * always counts the full selection. A cluster that is not answering is neither disabled nor
 * hidden: the grant is durable and works once the host is back, and a grant on a hidden cluster
 * could not be unticked.
 */
export function ClusterAccessSelector({
  clusters,
  selectedClusterIds,
  onToggle,
  listChecking = false,
}: ClusterAccessSelectorProps) {
  const [search, setSearch] = React.useState("");
  const safeClusterList = React.useMemo(
    () =>
      Array.isArray(clusters)
        ? clusters.filter((cluster) => cluster && typeof cluster.id === "string")
        : [],
    [clusters],
  );

  // An orphan row exists only while a grant names it, so unticking would unmount it mid-click and
  // a misclick could not be undone. Once shown it stays for the life of the dialog, which
  // unmounts on close, so nothing accumulates across visits.
  const seenOrphans = React.useRef<Set<string>>(new Set());
  const rows = React.useMemo(() => {
    const listed = safeClusterList.map(toRow);
    const known = new Set(listed.map((row) => row.id));
    for (const id of selectedClusterIds ?? []) {
      if (typeof id === "string" && id && !known.has(id)) seenOrphans.current.add(id);
    }
    const orphans = [...seenOrphans.current]
      .filter((id) => !known.has(id))
      .sort()
      .map((id) => orphanRow(id, listChecking));
    return [...listed, ...orphans];
  }, [safeClusterList, selectedClusterIds, listChecking]);

  // Reachable clusters lead.
  //
  // important: the order is recomputed only when the membership changes, never when a status
  // flips. A refetch (ex; on window refocus) would otherwise move a row under the pointer between
  // reading and clicking, and on a credentials control that grants the wrong cluster.
  const membershipKey = rows.map((row) => row.id).join(" ");
  const orderRef = React.useRef<{ key: string; ids: string[] }>({ key: "", ids: [] });
  if (orderRef.current.key !== membershipKey) {
    const ranked = [...rows].sort(
      (a, b) => (a.meta?.toneRank ?? 2) - (b.meta?.toneRank ?? 2) || a.name.localeCompare(b.name),
    );
    orderRef.current = { key: membershipKey, ids: ranked.map((row) => row.id) };
  }
  const frozenOrder = orderRef.current.ids;
  const ordered = React.useMemo(() => {
    const rank = new Map(frozenOrder.map((id, index) => [id, index]));
    return [...rows].sort((a, b) => (rank.get(a.id) ?? 0) - (rank.get(b.id) ?? 0));
  }, [rows, frozenOrder]);

  const filtered = React.useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return ordered;
    return ordered.filter((row) => {
      // The status word is searchable, so typing "offline" lists the clusters that are down.
      const haystack = [row.id, row.name, row.endpoint, row.typeLabel, row.meta?.label]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [ordered, search]);

  const showSearch = rows.length > SEARCH_THRESHOLD;
  const withStatus = ordered.filter((row) => row.meta && !row.orphan);
  const onlineCount = withStatus.filter((row) => row.meta?.reachable).length;
  // No status on any row means reachability itself is unavailable; it must never render as a
  // fleet that is down.
  const statusUnavailable = safeClusterList.length > 0 && withStatus.length === 0;

  const selected = new Set(selectedClusterIds ?? []);
  // Orphans are excluded: their cluster is not in this scope at all, so "takes effect once the
  // cluster is reachable" would be false for them. The orphan row explains itself.
  const unreachableSelected = ordered.filter(
    (row) =>
      !row.orphan && selected.has(row.id) && row.meta && row.meta.status !== ClusterStatus.ONLINE,
  );

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <Label>Cluster Access</Label>
        {!statusUnavailable && withStatus.length > 0 && (
          <p role="status" aria-live="polite" className="text-xs text-muted-foreground">
            {onlineCount} of {plural(withStatus.length, "cluster")} online
          </p>
        )}
      </div>
      {showSearch && (
        <SearchInput
          value={search}
          onChange={setSearch}
          placeholder="Search by name, endpoint, type, or status"
          label="Search clusters"
        />
      )}
      <div className="h-48 overflow-y-auto rounded-md border p-3">
        {rows.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">
            No clusters available.
          </p>
        ) : filtered.length === 0 ? (
          <p className="py-4 text-center text-sm text-muted-foreground">
            No clusters match &quot;{search}&quot;.
          </p>
        ) : (
          <div className="space-y-2">
            {filtered.map((row) => {
              const isSelected = selected.has(row.id);
              // The id is what a grant stores and what tables and URLs use, so it stays visible
              // when it differs from the name; two clusters can share one endpoint. One line,
              // joined the way use-node-picker-options builds its sublabel.
              const sublabel = [row.name !== row.id ? row.id : null, row.endpoint]
                .filter(Boolean)
                .join("  ");

              return (
                <label
                  key={row.id}
                  htmlFor={`cluster-${row.id}`}
                  className={cn(
                    "flex cursor-pointer items-start gap-3 rounded-md p-2 transition-colors",
                    isSelected
                      ? "border border-primary bg-primary/10"
                      : "bg-muted/50 hover:bg-muted",
                  )}
                >
                  <input
                    type="checkbox"
                    id={`cluster-${row.id}`}
                    checked={isSelected}
                    onChange={() => onToggle(row.id)}
                    className="h-4 w-4 shrink-0 rounded border-primary text-primary focus:ring-primary"
                  />
                  <div className="min-w-0 flex-1 space-y-0.5">
                    {/* Wraps instead of using a breakpoint: status and type sit beside the name
                        when the row has room and drop to their own line when it does not. */}
                    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                      <p className="break-words text-sm font-medium">{row.name}</p>
                      <span className="flex shrink-0 items-center gap-2">
                        {row.meta && (
                          <span className="text-xs text-muted-foreground">
                            <span className="sr-only">Status: </span>
                            {row.meta.label}
                          </span>
                        )}
                        {!row.orphan && (
                          <Badge variant="outline" className="text-xs">
                            {row.typeLabel}
                          </Badge>
                        )}
                      </span>
                    </div>
                    {sublabel && (
                      <p className="break-all font-mono text-xs text-muted-foreground">
                        {sublabel}
                      </p>
                    )}
                    {row.orphan && (
                      <p className="text-xs text-muted-foreground">
                        {listChecking
                          ? "Checking cluster list"
                          : "Not in your cluster list. Untick to remove this grant."}
                      </p>
                    )}
                  </div>
                </label>
              );
            })}
          </div>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Selected: {plural(selectedClusterIds.length, "cluster")}
      </p>
      {statusUnavailable && (
        <p role="status" className="flex items-start gap-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Cluster reachability is unavailable. Clusters are listed without status.</span>
        </p>
      )}
      {/* Not NodeStatusNotice, whose copy says creating will fail. A grant is stored either way
          and starts working when the cluster does. */}
      {unreachableSelected.length > 0 && (
        <p role="status" className="flex items-start gap-2 text-xs">
          <AlertTriangle
            aria-hidden
            className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-pending"
          />
          <span className="text-muted-foreground">
            {unreachableSelected.length === 1
              ? `${unreachableSelected[0].name} is not answering right now. `
              : `${unreachableSelected.length} selected clusters are not answering right now. `}
            The grant is saved either way and takes effect once the cluster is reachable.
          </span>
        </p>
      )}
    </div>
  );
}
