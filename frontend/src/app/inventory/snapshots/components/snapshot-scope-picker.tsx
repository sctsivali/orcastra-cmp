"use client";

import { Label } from "@/components/ui/label";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { useSnapshotProjectOptions } from "@/hooks/use-snapshot-project-options";

/**
 * Cluster and project narrow a fleet-wide listing; neither has to be chosen first.
 *
 * Nothing is disabled while loading (the scope-picker rule in node-filter-select.tsx): an
 * operator often opens the page for a broken cluster, and the fleet-wide default is valid before
 * any request resolves. Only a read failure disables a picker. SearchableSelect shows a
 * placeholder only when nothing is selected, so a muted line says when projects are being read.
 */

/** The absence of a value, which is also how the URL spells it: no key at all. */
export const ALL_CLUSTERS = "";
export const ALL_PROJECTS = "";

// Module scope, not inline: `leading` is a useMemo dependency inside useNodePickerOptions, so a
// fresh array each render rebuilds every option on every keystroke.
const ALL_CLUSTERS_OPTION: SearchableSelectOption[] = [
  { value: ALL_CLUSTERS, label: "All Clusters" },
];
const ALL_PROJECTS_OPTION: SearchableSelectOption = {
  value: ALL_PROJECTS,
  label: "All Projects",
};

interface SnapshotScopePickerProps {
  host: string;
  project: string;
  onHostChange: (host: string) => void;
  onProjectChange: (project: string) => void;
}

export function SnapshotScopePicker({
  host,
  project,
  onHostChange,
  onProjectChange,
}: SnapshotScopePickerProps) {
  const nodePicker = useNodePickerOptions({ leading: ALL_CLUSTERS_OPTION });
  const projectPicker = useSnapshotProjectOptions(host);

  const projectOptions = [ALL_PROJECTS_OPTION, ...projectPicker.options];
  // Only while the list still holds just the "All" row, so a warm cache never flashes it.
  const readingProjects = projectPicker.isLoading && projectOptions.length === 1;

  return (
    <div className="flex flex-wrap items-end gap-4">
      <div className="min-w-[220px] space-y-2">
        <Label htmlFor="snapshot-cluster">Cluster</Label>
        <SearchableSelect
          id="snapshot-cluster"
          value={host}
          onValueChange={onHostChange}
          options={nodePicker.isError ? [] : nodePicker.options}
          placeholder={nodePicker.isError ? "Clusters unavailable" : "All Clusters"}
          searchPlaceholder="Search clusters"
          emptyText={
            nodePicker.isError ? "The cluster list could not be read" : "No cluster found"
          }
          disabled={nodePicker.isError}
          ariaLabel="Cluster"
        />
        {nodePicker.isError && (
          <p className="text-sm text-destructive">
            The cluster list could not be read, so this is not an empty fleet.
          </p>
        )}
      </div>

      <div className="min-w-[220px] space-y-2">
        <Label htmlFor="snapshot-project">Project</Label>
        <SearchableSelect
          id="snapshot-project"
          value={project}
          onValueChange={onProjectChange}
          // On a failed read the list is empty rather than falling back to a single option,
          // which would claim the cluster has only that one project.
          options={projectPicker.isError ? [] : projectOptions}
          placeholder={projectPicker.isError ? "Projects unavailable" : "All Projects"}
          searchPlaceholder="Search projects"
          emptyText={
            projectPicker.isError ? "The project list could not be read" : "No project found"
          }
          disabled={projectPicker.isError}
          ariaLabel="Project"
        />
        {projectPicker.isError ? (
          <p className="text-sm text-destructive">
            The project list could not be read, so this is not a cluster with one project.
          </p>
        ) : readingProjects ? (
          <p className="text-sm text-muted-foreground">Reading projects</p>
        ) : null}
      </div>
    </div>
  );
}
