"use client";

import * as React from "react";

import { useAllProjects } from "@/hooks/use-lxd";
import type { AvailableCluster, ProjectListResponse } from "@/types";

export interface ProjectScopeOption {
  /**
   * The project name, spelled exactly as the cluster spells it.
   *
   * important: this string is what lands in ClusterPermission.projects, and the backend matches
   * it literally with no folding of any kind. It is never lowercased, trimmed or reformatted
   * anywhere between here and the payload. This fleet carries both `Ryo-Labs` and `ryo-labs`.
   */
  name: string;
  /** First non-empty description found across the covering clusters. */
  description: string | null;
  /** Ids of selected clusters that report this project. Never empty. */
  presentOn: string[];
  /** Selected clusters that answered and did not report it. Empty means full coverage. */
  missingFrom: string[];
  /** Display names for `missingFrom`, in the same order. */
  missingFromLabels: string[];
}

export interface UseProjectScopeOptionsParams {
  /** Cluster ids ticked in the cluster selector. Tick order is preserved. */
  selectedClusterIds: readonly string[];
  /** Resolves a cluster id to the name the operator sees. No extra query needed. */
  clusters: readonly AvailableCluster[];
  /** Names already picked, so orphans are computed here rather than in two places. */
  selectedProjects: readonly string[];
  /**
   * Skip the fetch. Defaults to true.
   *
   * note: the dialog passes `projectScope === "specific"`. Without it, opening the dialog under
   * any scope would fan the projects read out across the whole org for a field nobody is
   * looking at.
   */
  enabled?: boolean;
}

export interface UseProjectScopeOptionsResult {
  /** Union across the answering selected clusters, `default` first then alphabetical. */
  options: ProjectScopeOption[];
  /** How many selected clusters answered. Zero means nothing in `options` is trustworthy. */
  answeredCount: number;
  /** Display names of selected clusters that did not answer. */
  unansweredClusterLabels: string[];
  /**
   * Picked names that no answering selected cluster reports.
   *
   * important: empty whenever a selected cluster stayed silent. A name known only to a cluster
   * that is down is not a typo, and blocking the form on one would turn an outage into operator
   * error. The backend draws the same line.
   */
  orphanedProjects: string[];
  /** Picked names an answering cluster is missing, but which another cluster does have. */
  partiallyCoveredProjects: ProjectScopeOption[];
  isLoading: boolean;
  isError: boolean;
  refreshNow: () => Promise<void>;
}

const DEFAULT_PROJECT = "default";

/**
 * The project rows a "specific projects" API key grant may choose from.
 *
 * Sources the fleet-wide `/projects` read rather than one call per ticked cluster. That endpoint
 * is single-flighted and cached for 30s, three other surfaces already warm it, and it degrades in
 * band: a cluster that cannot be reached comes back labelled rather than missing. The per-cluster
 * endpoint answers 503 instead, which is the one case this picker most needs to render calmly.
 */
export function useProjectScopeOptions({
  selectedClusterIds,
  clusters,
  selectedProjects,
  enabled = true,
}: UseProjectScopeOptionsParams): UseProjectScopeOptionsResult {
  const query = useAllProjects({ enabled });

  const labelOf = React.useCallback(
    (clusterId: string) =>
      clusters.find((cluster) => cluster?.id === clusterId)?.name || clusterId,
    [clusters],
  );

  const byHost = React.useMemo(() => {
    const map = new Map<string, ProjectListResponse>();
    for (const row of query.data ?? []) {
      if (row && typeof row.host === "string") map.set(row.host, row);
    }
    return map;
  }, [query.data]);

  const { options, answeredCount, unansweredClusterLabels } = React.useMemo(() => {
    const answered: string[] = [];
    const unanswered: string[] = [];
    const acc = new Map<string, { description: string | null; presentOn: string[] }>();

    for (const clusterId of selectedClusterIds) {
      const row = byHost.get(clusterId);
      // important: a host the response never mentioned is UNANSWERED, not empty. Reading it as
      // "this cluster has no projects" would orphan every pick that lives only there and block
      // the form on a race between two reads. `reachable !== false` and not `!reachable`: the
      // field is optional and absent means reachable, so the other spelling blanks the picker
      // for a healthy fleet.
      if (!row || row.reachable === false) {
        unanswered.push(clusterId);
        continue;
      }
      answered.push(clusterId);
      for (const project of row.projects ?? []) {
        const name = project?.name;
        if (!name) continue;
        // important: `*` is the backend's wildcard for every project on the cluster. A cluster
        // that somehow reported a project with that name would let one tick silently widen the
        // grant from one project to all of them. LXD forbids the name; this does not rely on it.
        if (name === "*") continue;
        const entry = acc.get(name);
        if (entry) {
          entry.presentOn.push(clusterId);
          if (!entry.description && project.description) entry.description = project.description;
        } else {
          acc.set(name, { description: project.description ?? null, presentOn: [clusterId] });
        }
      }
    }

    const built: ProjectScopeOption[] = Array.from(acc, ([name, entry]) => {
      const missingFrom = answered.filter((id) => !entry.presentOn.includes(id));
      return {
        name,
        description: entry.description,
        presentOn: entry.presentOn,
        missingFrom,
        missingFromLabels: missingFrom.map(labelOf),
      };
    });

    // `default` is pinned first because it is the backend's own fallback and the narrowest
    // useful grant, so it should be one keystroke away rather than hunted for in a list of
    // thirty. cmdk re-scores by match once a query is typed, which correctly overrides this.
    built.sort((a, b) => {
      if (a.name === DEFAULT_PROJECT) return -1;
      if (b.name === DEFAULT_PROJECT) return 1;
      return a.name.localeCompare(b.name);
    });

    return {
      options: built,
      answeredCount: answered.length,
      unansweredClusterLabels: unanswered.map(labelOf),
    };
  }, [byHost, selectedClusterIds, labelOf]);

  const { orphanedProjects, partiallyCoveredProjects } = React.useMemo(() => {
    // Nothing answered, so nothing is known. Reporting orphans here would block a create
    // because a cluster is down, which is the outage-as-operator-error failure again.
    if (answeredCount === 0 || unansweredClusterLabels.length > 0) {
      return { orphanedProjects: [], partiallyCoveredProjects: [] };
    }
    const byName = new Map(options.map((option) => [option.name, option]));
    const orphaned: string[] = [];
    const partial: ProjectScopeOption[] = [];
    for (const name of selectedProjects) {
      const option = byName.get(name);
      if (!option) orphaned.push(name);
      else if (option.missingFrom.length > 0) partial.push(option);
    }
    return { orphanedProjects: orphaned, partiallyCoveredProjects: partial };
  }, [options, selectedProjects, answeredCount, unansweredClusterLabels]);

  return {
    options,
    answeredCount,
    unansweredClusterLabels,
    orphanedProjects,
    partiallyCoveredProjects,
    isLoading: query.isLoading,
    isError: query.isError,
    refreshNow: query.refreshNow,
  };
}
