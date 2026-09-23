"use client";

import { AlertTriangle, Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
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
import { LXD_NAME_PATTERN, type MigrationDraftState } from "./migration-wizard-model";

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
                  targetMember: intent === "copy" ? "" : draft.targetMember,
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
                <SelectItem value="move">
                  Staged move
                </SelectItem>
                <SelectItem value="copy">
                  Durable copy
                </SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              A staged move retains the stopped source for rollback. Copy leaves it active.
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
                  targetResourceName: resourceName,
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
              Preflight discovers every project workload and dependency across all source
              members. Partial project relocation is not supported.
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

  const sameLocation =
    draft.targetCluster === draft.sourceCluster &&
    draft.targetProject === draft.sourceProject &&
    (draft.scope === "project" || draft.targetResourceName === draft.resourceName) &&
    (!draft.targetMember || draft.targetMember === draft.sourceMember);
  const projectNameValid = LXD_NAME_PATTERN.test(draft.targetProject);
  const resourceNameValid = draft.scope === "project" || LXD_NAME_PATTERN.test(draft.targetResourceName);
  const memberOptions = [
    { value: "__auto__", label: "Automatic Placement" },
    ...targetMembers.map((member) => ({
      value: member.server_name,
      label: member.server_name,
      sublabel: `${member.status}${member.architecture ? ` · ${member.architecture}` : ""}`,
    })),
  ];
  const poolOptions = [
    { value: "__default__", label: "Project Default Pool" },
    ...targetPools.map((pool) => ({
      value: pool.name,
      label: pool.name,
      sublabel: `${pool.driver} · ${pool.status}`,
    })),
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="migration-target-cluster">Destination cluster</Label>
          <SearchableSelect
            id="migration-target-cluster"
            value={draft.targetCluster}
            onValueChange={(targetCluster) =>
              onChange({
                targetCluster,
                targetProject: "",
                targetMember: "",
                targetStoragePool: "",
                mappings: emptyMigrationMappings(),
                acknowledgements: [],
              })
            }
            options={clusterOptions}
            placeholder={inventoryLoading ? "Loading clusters" : "Choose a destination cluster"}
            searchPlaceholder="Search clusters"
            emptyText="No reachable cluster available"
            disabled={inventoryLoading}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="migration-target-project">Destination project</Label>
          <Input
            id="migration-target-project"
            value={draft.targetProject}
            onChange={(event) =>
              onChange({
                targetProject: event.target.value,
                mappings: emptyMigrationMappings(),
                acknowledgements: [],
              })
            }
            autoComplete="off"
            placeholder="New or empty project name"
            aria-invalid={draft.targetProject.length > 0 && !projectNameValid}
          />
          <p className="text-xs text-muted-foreground">
            The target must be new or verified empty. Project merge is not supported.
          </p>
          {draft.targetProject.length > 0 && !projectNameValid && (
            <p className="text-xs text-destructive">
              Use letters, digits, and dashes, starting with a letter or digit, up to 63 characters.
            </p>
          )}
        </div>
      </div>

      {draft.scope === "instance" && (
        <div className="space-y-2">
          <Label htmlFor="migration-target-resource">Destination instance name</Label>
          <Input
            id="migration-target-resource"
            value={draft.targetResourceName}
            onChange={(event) => onChange({ targetResourceName: event.target.value })}
            autoComplete="off"
            aria-invalid={draft.targetResourceName.length > 0 && !resourceNameValid}
          />
          {draft.targetResourceName.length > 0 && !resourceNameValid && (
            <p className="text-xs text-destructive">
              Use letters, digits, and dashes, starting with a letter or digit, up to 63 characters.
            </p>
          )}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        {draft.intent === "move" && (
          <div className="space-y-2">
            <Label htmlFor="migration-target-member">Destination member (optional)</Label>
            <SearchableSelect
              id="migration-target-member"
              value={draft.targetMember || "__auto__"}
              onValueChange={(value) => onChange({ targetMember: value === "__auto__" ? "" : value })}
              options={memberOptions}
              placeholder="Let the cluster schedule it"
              searchPlaceholder="Search members"
              emptyText="No member reported"
              disabled={!draft.targetCluster || resourceLoading}
            />
          </div>
        )}
        <div className="space-y-2">
          <Label htmlFor="migration-target-pool">Destination storage pool (optional)</Label>
          <SearchableSelect
            id="migration-target-pool"
            value={draft.targetStoragePool || "__default__"}
            onValueChange={(value) =>
              onChange({ targetStoragePool: value === "__default__" ? "" : value })
            }
            options={poolOptions}
            placeholder="Use the project default"
            searchPlaceholder="Search storage pools"
            emptyText="No storage pool reported"
            disabled={!draft.targetCluster || resourceLoading}
          />
        </div>
      </div>

      {sameLocation && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertTitle>Source and destination are identical</AlertTitle>
          <AlertDescription>
            Choose another cluster, project, member, or instance name before preflight.
          </AlertDescription>
        </Alert>
      )}

      {inventoryError && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertTitle>Destination inventory is incomplete</AlertTitle>
          <AlertDescription>{inventoryError}</AlertDescription>
        </Alert>
      )}

      <Alert>
        <Info className="h-4 w-4" aria-hidden />
        <AlertTitle>Destination names are validated by preflight</AlertTitle>
        <AlertDescription>
          Organization ownership, hypervisor family, capacity, security baseline, and target
          emptiness are enforced by the server.
        </AlertDescription>
      </Alert>
    </div>
  );
}
