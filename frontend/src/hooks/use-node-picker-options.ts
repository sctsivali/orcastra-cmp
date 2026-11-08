"use client";

import { useCallback, useMemo } from "react";

import { useNodeAvailability, useNodeNames } from "@/hooks/use-lxd";
import { clusterStatusMeta, clusterSublabel, type ClusterStatusMeta } from "@/lib/cluster-status";
import type { SearchableSelectOption } from "@/components/ui/searchable-select";
import { ClusterStatus, type ClusterAvailability } from "@/types/api";

/**
 * The one place node-picker options are built.
 *
 * Six create and upload forms each carried their own `(nodeNames || []).map(n => ({value: n,
 * label: n}))`, so none of them could say which clusters would actually answer. On the dev
 * fleet fourteen of sixteen clusters are unusable, and the picker offered all sixteen as
 * identical rows.
 */

export interface UseNodePickerOptionsParams {
  /** Restrict to one hypervisor family, ex; "microcloud". */
  clusterType?: string;
  /** Options pinned ahead of the clusters, ex; an "All clusters" row. */
  leading?: SearchableSelectOption[];
  /**
   * Disable clusters whose trust handshake never completed. Default true.
   *
   * That is a durable fact with no staleness risk, unlike reachability, and every create
   * against such a cluster fails with certainty. Offline clusters stay selectable: their
   * status is a claim about the recent past, and an operator often has to work on the one
   * that is broken.
   */
  disableUntrusted?: boolean;
  /** Disable every cluster that is known not to be online. Create mutations use this. */
  disableUnreachable?: boolean;
  /** Show the cluster endpoint as the option's second line. Default true. */
  showEndpoint?: boolean;
}

export interface UseNodePickerOptionsResult {
  options: SearchableSelectOption[];
  isLoading: boolean;
  isError: boolean;
  /** True only after the authoritative cluster-name read completed successfully. */
  namesLoaded: boolean;
  /** Status for one cluster, for the consequence line under the field. */
  statusOf: (nodeName: string) => ClusterStatusMeta | undefined;
  /** The raw availability row, when the caller wants the reason text. */
  availabilityOf: (nodeName: string) => ClusterAvailability | undefined;
  /** What the picker shows for this cluster, so a warning names what the operator sees. */
  labelOf: (nodeName: string) => string;
  /** True when reachability could not be established. Degrade, never invent Offline. */
  statusUnavailable: boolean;
  statusUpdatedAt: number | null;
  reachableCount: number;
  totalCount: number;
  /** Options an operator can actually pick. Zero is a state the form has to explain. */
  selectableCount: number;
  refreshNow: () => Promise<unknown>;
  /** Why the names read failed, for the notice under the picker. */
  namesError: unknown;
  /** Re-read the names. refreshNow only rechecks reachability. */
  retryNames: () => Promise<unknown>;
}

/** The hypervisor type availability reports for one cluster ("LXD", "Incus"...), null while unknown. */
export function useClusterType(host: string | undefined): string | null {
  const { byName } = useAvailabilityByName();
  return host ? byName.get(host)?.node_type ?? null : null;
}

/** Availability rows keyed by cluster id. */
function useAvailabilityByName() {
  const availability = useNodeAvailability();
  const byName = useMemo(() => {
    const map = new Map<string, ClusterAvailability>();
    for (const row of availability.data?.clusters ?? []) map.set(row.name, row);
    return map;
  }, [availability.data]);
  return { availability, byName };
}

// note: the trigger shows the display name, so a warning that names the cluster id instead
// reads as being about a different cluster. blitzkrieg renders as "Hetzner".
function useLabelOf(byName: Map<string, ClusterAvailability>) {
  return useCallback(
    (nodeName: string) => byName.get(nodeName)?.display_name || nodeName,
    [byName],
  );
}

/**
 * What the console calls a cluster, its display name or else its id, for a surface that names
 * clusters without offering a choice between them (the Monitoring cards and pools).
 *
 * note: availability only. The names list behind useNodePickerOptions is Partner-only, and
 * Monitoring is a Tenant page too; availability is Tenant-readable and keeps display_name.
 */
export function useClusterLabel(): (nodeName: string) => string {
  const { byName } = useAvailabilityByName();
  return useLabelOf(byName);
}

export function useNodePickerOptions(
  params: UseNodePickerOptionsParams = {},
): UseNodePickerOptionsResult {
  const {
    clusterType,
    leading,
    disableUntrusted = true,
    disableUnreachable = false,
    showEndpoint = true,
  } = params;

  const names = useNodeNames();
  const { availability, byName } = useAvailabilityByName();

  // Reachability failing must not look like the fleet failing. With no status we render the
  // names plainly rather than painting every cluster Offline, which would turn a monitoring
  // outage into an apparent total outage across six forms at once.
  const statusUnavailable = !availability.isLoading && !availability.data;

  const options = useMemo(() => {
    // important: names drive membership and availability left-joins onto it, never the
    // reverse. If availability drove the list, one 500 would empty every node picker.
    const wanted = clusterType?.toLowerCase();
    const rows = (names.data ?? [])
      // A cluster we have no availability row for is not evidence of the wrong type, so it
      // stays: filtering it out would hide a cluster because a status probe was slow.
      .filter((name) => {
        if (!wanted) return true;
        const row = byName.get(name);
        return !row || (row.node_type ?? "").toLowerCase() === wanted;
      })
      .map((name) => {
      const row = byName.get(name);
      const meta = row ? clusterStatusMeta(row.status) : undefined;
      const untrusted =
        row?.status === ClusterStatus.NOT_AUTHORIZED ||
        row?.status === ClusterStatus.PENDING;

      // The label is the display name, which is what an operator named the cluster. The id is
      // what a create request sends and what every table and URL uses, so it has to stay
      // visible: blitz001 and blitz01 are called "BSD Lab" and "VDC" and share one endpoint,
      // so with the id hidden the endpoint could not tell them apart either.
      const label = row?.display_name || name;
      const sublabel = clusterSublabel(
        name,
        row?.display_name,
        showEndpoint ? row?.endpoint : undefined,
      );

      return {
        value: name,
        label,
        sublabel,
        status: meta
          ? { label: meta.label, tone: meta.tone }
          : undefined,
        statusReason: row?.reason ?? undefined,
        disabled:
          (disableUntrusted && untrusted) ||
          (disableUnreachable && Boolean(meta && !meta.reachable)),
        // kept out of the option so the sort below can read it without a second lookup
        _rank: meta?.toneRank ?? 2,
      };
    });


    // Reachable clusters lead. With two usable clusters in sixteen, alphabetical order buries
    // both of them mid-list.
    //
    // note: cmdk re-sorts by match score once a query is typed, which overrides this. That is
    // correct, not a bug: relevance should beat status the moment the operator is searching.
    rows.sort((a, b) => a._rank - b._rank || a.label.localeCompare(b.label));

    const cleaned: SearchableSelectOption[] = rows.map(({ _rank, ...opt }) => opt);
    return leading ? [...leading, ...cleaned] : cleaned;
  }, [
    names.data,
    byName,
    clusterType,
    disableUntrusted,
    disableUnreachable,
    showEndpoint,
    leading,
  ]);

  const statusOf = useCallback(
    (nodeName: string) => {
      const row = byName.get(nodeName);
      return row ? clusterStatusMeta(row.status) : undefined;
    },
    [byName],
  );

  const availabilityOf = useCallback(
    (nodeName: string) => byName.get(nodeName),
    [byName],
  );

  const labelOf = useLabelOf(byName);

  const refreshNow = useCallback(async () => {
    // Only an explicit recheck forces. The forced-read budget is 12 per minute per user, and
    // six pickers forcing on open would exhaust it and turn every later read into a 429.
    await availability.refreshNow?.();
  }, [availability]);

  const retryNames = useCallback(async () => {
    await names.refetch();
  }, [names]);

  return {
    options,
    isLoading: names.isLoading,
    isError: names.isError,
    namesLoaded: names.isSuccess,
    statusOf,
    availabilityOf,
    labelOf,
    statusUnavailable,
    statusUpdatedAt: availability.dataUpdatedAt ?? null,
    reachableCount: availability.data?.online_count ?? 0,
    totalCount: availability.data?.total_count ?? (names.data?.length ?? 0),
    selectableCount: options.filter((o) => !o.disabled).length,
    refreshNow,
    namesError: names.error,
    retryNames,
  };
}
