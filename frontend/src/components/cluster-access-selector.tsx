"use client";

import * as React from "react";
import { AlertTriangle, Info } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { clusterStatusMeta, type ClusterStatusMeta } from "@/lib/cluster-status";
import { cn, formatHypervisorType } from "@/lib/utils";
import { ClusterStatus, type AvailableCluster } from "@/types";

// note: search box appears only past this count so short cluster lists stay uncluttered
const SEARCH_THRESHOLD = 6;

interface ClusterAccessSelectorProps {
  clusters: AvailableCluster[];
  selectedClusterIds: string[];
  onToggle: (clusterId: string) => void;
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
    // important: undefined, not clusterStatusMeta(undefined). That resolves to Unknown, which is
    // a claim about the cluster; this is a gap in what we know and must render as nothing.
    meta: cluster.status ? clusterStatusMeta(cluster.status) : undefined,
    orphan: false,
  };
}

function orphanRow(clusterId: string): Row {
  return {
    id: clusterId,
    name: clusterId,
    endpoint: "",
    typeLabel: formatHypervisorType(null),
    meta: clusterStatusMeta(undefined),
    orphan: true,
  };
}

/**
 * Multi-select cluster picker used when scoping an API key.
 *
 * Selection is tracked by id, so a cluster that is selected and then filtered out of view stays
 * selected; the counter below always reflects the full selection, not just what is visible.
 *
 * Two things it deliberately does not do. It does not disable a cluster that is not answering:
 * this grants durable permission rather than creating anything, so a host being down right now is
 * not a reason to refuse the grant, and the credential works the moment it comes back. And it
 * does not hide one either, because a grant on a cluster that is not listed cannot be unticked,
 * and access nobody can revoke is the worse failure on a credentials surface.
 */
export function ClusterAccessSelector({
  clusters,
  selectedClusterIds,
  onToggle,
}: ClusterAccessSelectorProps) {
  const [search, setSearch] = React.useState("");
  const safeClusterList = React.useMemo(
    () =>
      Array.isArray(clusters)
        ? clusters.filter((cluster) => cluster && typeof cluster.id === "string")
        : [],
    [clusters],
  );

  // An orphan row exists because a grant names it, so unticking one would normally unmount it
  // mid-click: the row an operator was looking at vanishes, and a misclick cannot be undone
  // before saving. Once shown, it stays for the life of the dialog and behaves like every other
  // row. The dialog unmounts on close, so nothing accumulates across visits.
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
      .map(orphanRow);
    return [...listed, ...orphans];
  }, [safeClusterList, selectedClusterIds]);

  // Reachable clusters lead, so the ones an operator can act on are not buried mid-list.
  //
  // important: the order is recomputed only when the membership changes, never when a status
  // flips. Status arrives on a refetch, and a window refocus is enough to trigger one, so
  // re-sorting a live list moves a row out from under the pointer between the eye and the click.
  // On a control that grants credentials that is the wrong cluster, not a cosmetic glitch.
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
      // The status word is in the haystack on purpose: typing "offline" narrows the list to the
      // broken clusters, which is how an operator finds them rather than only avoids them.
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
  // Absent on every row means reachability itself is unavailable, which is a different statement
  // from a fleet that is down and must never be rendered as one.
  const statusUnavailable = safeClusterList.length > 0 && withStatus.length === 0;

  const selected = new Set(selectedClusterIds ?? []);
  // important: orphans are excluded. Their status is Unknown, so they would otherwise be swept
  // into the sentence below and told the grant "takes effect once the cluster is reachable",
  // which is false: the cluster is not in this scope at all and reachability is not what is
  // missing. The row says the true thing about itself.
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
            {onlineCount} of {withStatus.length} clusters online
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
              // The id is what a grant stores and what every table and URL uses, so it stays
              // visible when it differs from the name. Two clusters on this fleet share one
              // endpoint, so with the id hidden the endpoint cannot tell them apart either.
              //
              // note: one line, not two. Density is the product here, and use-node-picker-options
              // already joins the same pair into a single sublabel.
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
                    {/* Wrapping rather than a breakpoint. The status and the type sit beside the
                        name when there is room and drop to their own line when there is not, so
                        this reads the width it actually has instead of the viewport's. */}
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
                        Not in your cluster list. Untick to remove this grant.
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
        Selected: {selectedClusterIds.length} cluster(s)
      </p>
      {statusUnavailable && (
        <p role="status" className="flex items-start gap-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Cluster reachability is unavailable. Clusters are listed without status.</span>
        </p>
      )}
      {/* note: not NodeStatusNotice. Its copy says creating here will fail, which is true on a
          create form and false here: the grant is stored either way and starts working when the
          cluster does. Same visual grammar, honest sentence. */}
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
