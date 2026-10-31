"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import type { ClusterMember, StoragePoolInfo } from "@/types/api";
import { emptyMigrationMappings } from "@/types/migrations";
import { SummaryField } from "./migration-summary-field";
import { PROJECT_MODE_LABEL, projectDestinationProblem, type MigrationDraftState } from "./migration-wizard-model";

const AUTO_MEMBER = "__auto__";
const DEFAULT_POOL = "__default__";

/** Destination fields of a whole-project relocation. The mode decides which ones are editable. */
export function MigrationProjectDestination({
  draft,
  onChange,
  clusterOptions,
  existingProjects,
  targetMembers,
  targetPools,
  inventoryLoading,
  resourceLoading,
}: {
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
  clusterOptions: SearchableSelectOption[];
  existingProjects: string[];
  targetMembers: ClusterMember[];
  targetPools: StoragePoolInfo[];
  inventoryLoading: boolean;
  resourceLoading: boolean;
}) {
  const mode = draft.projectMode;
  const problem = projectDestinationProblem(draft, existingProjects);
  const incomplete =
    (mode === "member" && !draft.targetMember) || (mode !== "member" && (!draft.targetCluster || !draft.targetProject.trim()));
  const sourceCluster = clusterOptions.find((option) => option.value === draft.sourceCluster);
  const destinationClusters = clusterOptions.map((option) =>
    option.value === draft.sourceCluster
      ? { ...option, disabled: true, statusReason: "The source cluster. Rename the project on it instead" }
      : option,
  );
  const memberOptions = targetMembers.map((member) => ({
    value: member.server_name,
    label: member.server_name,
    sublabel: `${member.status}${member.architecture ? ` · ${member.architecture}` : ""}`,
  }));
  const poolOptions = [
    { value: DEFAULT_POOL, label: "Same Pool or Project Default" },
    ...targetPools.map((pool) => ({ value: pool.name, label: pool.name, sublabel: `${pool.driver} · ${pool.status}` })),
  ];

  return (
    <div className="space-y-4">
      <p className="text-sm font-medium">{PROJECT_MODE_LABEL[mode]}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        {mode === "cluster" ? (
          <div className="space-y-2">
            <Label htmlFor="migration-target-cluster">Destination Cluster</Label>
            <SearchableSelect
              id="migration-target-cluster"
              value={draft.targetCluster}
              onValueChange={(targetCluster) =>
                onChange({
                  targetCluster,
                  targetMember: "",
                  targetStoragePool: "",
                  mappings: emptyMigrationMappings(),
                  acknowledgements: [],
                })
              }
              options={destinationClusters}
              placeholder={inventoryLoading ? "Loading clusters" : "Choose a destination cluster"}
              searchPlaceholder="Search clusters"
              emptyText="No reachable cluster available"
              disabled={inventoryLoading}
            />
          </div>
        ) : (
          <SummaryField label="Cluster" value={sourceCluster?.label ?? draft.sourceCluster} />
        )}
        {mode === "member" ? (
          <SummaryField label="Project" value={draft.sourceProject} mono />
        ) : (
          <div className="space-y-2">
            <Label htmlFor="migration-target-project-new">
              {mode === "rename" ? "New Project Name" : "Project Name on the Destination"}
            </Label>
            <Input
              id="migration-target-project-new"
              value={draft.targetProject}
              onChange={(event) => onChange({ targetProject: event.target.value, acknowledgements: [] })}
              autoComplete="off"
              aria-invalid={!!problem && !incomplete}
              aria-describedby="migration-project-destination-problem"
            />
          </div>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {(mode === "member" || targetMembers.length > 1) && (
          <div className="space-y-2">
            <Label htmlFor="migration-target-member">
              {mode === "member" ? "Destination Member" : "Destination Member (Optional)"}
            </Label>
            <SearchableSelect
              id="migration-target-member"
              value={draft.targetMember || (mode === "member" ? "" : AUTO_MEMBER)}
              onValueChange={(value) => onChange({ targetMember: value === AUTO_MEMBER ? "" : value, acknowledgements: [] })}
              options={mode === "member" ? memberOptions : [{ value: AUTO_MEMBER, label: "Automatic Placement" }, ...memberOptions]}
              placeholder={mode === "member" ? "Choose a member" : "Let the cluster place it"}
              searchPlaceholder="Search members"
              emptyText="No member reported"
              disabled={!draft.targetCluster || resourceLoading}
            />
            {mode === "member" && (
              <p className="text-xs text-muted-foreground">
                Workloads already on this member stay where they are.
              </p>
            )}
          </div>
        )}
        {mode !== "member" && (
          <div className="space-y-2">
            <Label htmlFor="migration-target-pool">Destination Storage Pool (Optional)</Label>
            <SearchableSelect
              id="migration-target-pool"
              value={draft.targetStoragePool || DEFAULT_POOL}
              onValueChange={(value) => onChange({ targetStoragePool: value === DEFAULT_POOL ? "" : value })}
              options={poolOptions}
              placeholder="Same pool or project default"
              searchPlaceholder="Search storage pools"
              emptyText="No storage pool reported"
              disabled={!draft.targetCluster || resourceLoading}
            />
            <p className="text-xs text-muted-foreground">
              Applies to every root disk, in the instances and in the cloned profiles.
            </p>
          </div>
        )}
      </div>

      {problem && (
        <p
          id="migration-project-destination-problem"
          className={incomplete ? "text-xs text-muted-foreground" : "text-xs text-destructive"}
        >
          {problem}
        </p>
      )}
    </div>
  );
}
