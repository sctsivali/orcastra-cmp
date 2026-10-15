"use client";

import { AlertTriangle, Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { clusterStatusMeta, clusterSublabel } from "@/lib/cluster-status";
import type {
  ClusterInfo,
  ClusterMember,
  ProjectListResponse,
  StoragePoolInfo,
  VMInfo,
} from "@/types/api";
import {
  emptyMigrationMappings,
  type MigrationIntent,
  type MigrationScope,
} from "@/types/migrations";
import { MigrationDestinationForm } from "./migration-destination-form";
import { defaultCopyName, type MigrationDraftState } from "./migration-wizard-model";

interface MigrationSourceDestinationProps {
  stage: "source" | "destination";
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
  clusters: ClusterInfo[];
  projects: ProjectListResponse[];
  instances: VMInfo[];
  targetMembers: ClusterMember[];
  targetPools: StoragePoolInfo[];
  inventoryLoading: boolean;
  resourceLoading: boolean;
  inventoryError?: string | null;
}

export function MigrationSourceDestination({
  stage,
  draft,
  onChange,
  clusters,
  projects,
  instances,
  targetMembers,
  targetPools,
  inventoryLoading,
  resourceLoading,
  inventoryError,
}: MigrationSourceDestinationProps) {
  const clusterOptions = clusters.map((cluster) => {
    const status = clusterStatusMeta(cluster.status);
    return {
      value: cluster.cluster_id,
      label: cluster.display_name || cluster.cluster_id,
      sublabel: clusterSublabel(cluster.cluster_id, cluster.display_name, cluster.endpoint),
      badge: cluster.cluster_type ?? undefined,
      status,
      disabled: !status.reachable,
      statusReason: cluster.error_message || `${status.label} clusters cannot accept a migration`,
    };
  });

  const sourceProjects = projects
    .filter((entry) => entry.host === draft.sourceCluster && entry.reachable !== false)
    .flatMap((entry) => entry.projects)
    .map((project) => ({ value: project.name, label: project.name }));
  const instanceOptions = instances.map((instance) => ({
    value: instance.name,
    label: instance.name,
    sublabel: `${instance.type === "virtual-machine" ? "VM" : "Container"} · ${instance.status}${instance.location ? ` · ${instance.location}` : ""}`,
  }));

  if (stage === "source") {
    return (
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="migration-scope">Migration scope</Label>
            <Select
              value={draft.scope}
              onValueChange={(value) =>
                onChange({
                  scope: value as MigrationScope,
                  resourceName: "",
                  sourceMember: "",
                  targetResourceName: "",
                  mappings: emptyMigrationMappings(),
                  acknowledgements: [],
                })
              }
            >
              <SelectTrigger id="migration-scope">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="instance">One instance</SelectItem>
                <SelectItem value="project">Entire project environment</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="migration-intent">Intent</Label>
            <Select
              value={draft.intent}
              onValueChange={(value) => {
                const intent = value as MigrationIntent;
                onChange({
                  intent,
                  targetResourceName:
                    intent === "copy" && draft.targetResourceName === draft.resourceName && draft.resourceName
                      ? defaultCopyName(draft.resourceName)
                      : draft.targetResourceName,
                  checkpointRequested: false,
                  externalCutoverRequired: false,
                  acknowledgements: [],
                });
              }}
            >
              <SelectTrigger id="migration-intent">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="move">Move</SelectItem>
                <SelectItem value="copy">Copy</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Move keeps the stopped source for a retention window so you can roll back. Copy
              creates a new, independent instance.
            </p>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="migration-source-cluster">Source cluster</Label>
            <SearchableSelect
              id="migration-source-cluster"
              value={draft.sourceCluster}
              onValueChange={(sourceCluster) =>
                onChange({
                  sourceCluster,
                  sourceProject: "",
                  resourceName: "",
                  sourceMember: "",
                  targetResourceName: "",
                  mappings: emptyMigrationMappings(),
                  acknowledgements: [],
                })
              }
              options={clusterOptions}
              placeholder={inventoryLoading ? "Loading clusters" : "Choose a source cluster"}
              searchPlaceholder="Search clusters"
              emptyText="No reachable cluster available"
              disabled={inventoryLoading}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="migration-source-project">Source project</Label>
            <SearchableSelect
              id="migration-source-project"
              value={draft.sourceProject}
              onValueChange={(sourceProject) =>
                onChange({
                  sourceProject,
                  resourceName: "",
                  sourceMember: "",
                  targetResourceName: "",
                  mappings: emptyMigrationMappings(),
                  acknowledgements: [],
                })
              }
              options={sourceProjects}
              placeholder="Choose a source project"
              searchPlaceholder="Search projects"
              emptyText="No project available on this cluster"
              disabled={!draft.sourceCluster || inventoryLoading}
            />
          </div>
        </div>

        {draft.scope === "instance" ? (
          <div className="space-y-2">
            <Label htmlFor="migration-resource-name">Instance name</Label>
            <SearchableSelect
              id="migration-resource-name"
              value={draft.resourceName}
              onValueChange={(resourceName) => {
                const selected = instances.find((instance) => instance.name === resourceName);
                onChange({
                  resourceName,
                  sourceMember: selected?.location ?? "",
                  targetResourceName:
                    draft.intent === "copy"
                      ? defaultCopyName(resourceName)
                      : resourceName,
                  mappings: emptyMigrationMappings(),
                  acknowledgements: [],
                });
              }}
              options={instanceOptions}
              placeholder={resourceLoading ? "Loading instances" : "Choose an instance"}
              searchPlaceholder="Search instances"
              emptyText="No instance found in this project"
              disabled={!draft.sourceProject || resourceLoading}
            />
          </div>
        ) : (
          <Alert>
            <Info className="h-4 w-4" aria-hidden />
            <AlertTitle>The complete project environment is in scope</AlertTitle>
            <AlertDescription>
              Preflight discovers every workload and dependency of the project. Whole-project
              relocation is not available on this deployment yet, so this plan is review only.
            </AlertDescription>
          </Alert>
        )}

        {inventoryError && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertTitle>Inventory is incomplete</AlertTitle>
            <AlertDescription>{inventoryError}</AlertDescription>
          </Alert>
        )}
      </div>
    );
  }

  return (
    <MigrationDestinationForm
      draft={draft}
      onChange={onChange}
      clusterOptions={clusterOptions}
      projects={projects}
      targetMembers={targetMembers}
      targetPools={targetPools}
      inventoryLoading={inventoryLoading}
      resourceLoading={resourceLoading}
      inventoryError={inventoryError}
    />
  );
}
