"use client";

import { useMemo } from "react";

import { useAllProjects } from "@/hooks/use-lxd";
import type { SearchableSelectOption } from "@/components/ui/searchable-select";

/**
 * Project names for the Snapshots scope picker, from the cached fleet-wide project read.
 *
 * `useAllProjects` goes through the cached, single-flighted fan-out, so the list is in hand when
 * the page opens and narrowing by cluster is a filter rather than a fetch. With Cluster set to
 * All the list is the union across the fleet, which a per-cluster read cannot produce.
 *
 * Not `useProjectScopeOptions`: that hook backs an API-key permission grant and carries orphan
 * and partial-coverage reporting for it.
 */

const DEFAULT_PROJECT = "default";

export interface SnapshotProjectOptions {
  options: SearchableSelectOption[];
  isLoading: boolean;
  isError: boolean;
  /** Clusters that did not answer, so the list may be missing their projects. */
  unansweredHosts: string[];
}

export function useSnapshotProjectOptions(host: string): SnapshotProjectOptions {
  const query = useAllProjects();

  return useMemo(() => {
    const rows = query.data ?? [];
    const names = new Set<string>();
    const unansweredHosts: string[] = [];

    for (const row of rows) {
      if (host && row.host !== host) continue;
      // important: `reachable !== false`, not `!reachable`. The field is optional and absent
      // means reachable, so the other spelling empties the list for a healthy fleet.
      if (row.reachable === false) {
        unansweredHosts.push(row.host);
        continue;
      }
      for (const project of row.projects ?? []) {
        const name = project?.name;
        if (!name) continue;
        // `*` is the backend's wildcard for every project on a cluster. LXD forbids it as a real
        // name; this does not rely on that.
        if (name === "*") continue;
        names.add(name);
      }
    }

    // `default` first because it is the backend's own fallback and the one an operator reaches
    // for most. cmdk re-scores by match once a query is typed, which overrides this.
    const sorted = Array.from(names).sort((a, b) => {
      if (a === DEFAULT_PROJECT) return -1;
      if (b === DEFAULT_PROJECT) return 1;
      return a.localeCompare(b);
    });

    return {
      options: sorted.map((name) => ({ value: name, label: name })),
      isLoading: query.isLoading,
      isError: query.isError,
      unansweredHosts,
    };
  }, [query.data, query.isLoading, query.isError, host]);
}
