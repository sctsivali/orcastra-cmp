"use client";

import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";

interface NodeOption {
  value: string;
  label: string;
  disabled?: boolean;
  description?: string;
}

interface SnapshotScopePickerProps {
  host: string;
  project: string;
  onHostChange: (host: string) => void;
  onProjectChange: (project: string) => void;
  nodeOptions: NodeOption[];
  nodesLoading: boolean;
  /** A failed cluster read must not render as an empty fleet. */
  nodesError?: boolean;
  projects: Array<{ name: string }>;
  projectsLoading: boolean;
}

/**
 * Cluster and project are the read's scope, not a filter over an already-fetched list.
 *
 * The listing is one call per cluster and project on purpose. Sweeping the whole fleet on every
 * page load would fan out across every registered cluster, and a partial failure in that shape
 * reads as an empty result rather than an incomplete one.
 */
export function SnapshotScopePicker({
  host,
  project,
  onHostChange,
  onProjectChange,
  nodeOptions,
  nodesLoading,
  nodesError = false,
  projects,
  projectsLoading,
}: SnapshotScopePickerProps) {
  const projectOptions = projects.map((p) => ({ value: p.name, label: p.name }));

  return (
    <div className="flex flex-wrap items-end gap-4">
      <div className="space-y-2 min-w-[220px]">
        <Label htmlFor="snapshot-cluster">Cluster</Label>
        <SearchableSelect
          id="snapshot-cluster"
          value={host}
          onValueChange={onHostChange}
          options={nodeOptions}
          placeholder={
            nodesLoading
              ? "Loading clusters"
              : nodesError
                ? "Clusters unavailable"
                : "Choose a cluster"
          }
          searchPlaceholder="Search clusters"
          emptyText={nodesError ? "The cluster list could not be read" : "No cluster available"}
          disabled={nodesLoading || nodesError}
        />
        {nodesError && (
          <p className="text-sm text-destructive">
            The cluster list could not be read, so this is not an empty fleet. Retry shortly.
          </p>
        )}
      </div>

      <div className="space-y-2 min-w-[220px]">
        <Label htmlFor="snapshot-project">Project</Label>
        <SearchableSelect
          id="snapshot-project"
          value={project}
          onValueChange={onProjectChange}
          options={projectOptions.length > 0 ? projectOptions : [{ value: "default", label: "default" }]}
          placeholder={projectsLoading ? "Loading projects" : "Choose a project"}
          searchPlaceholder="Search projects"
          emptyText="No project available"
          disabled={!host || projectsLoading}
        />
      </div>
    </div>
  );
}
