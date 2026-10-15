"use client";

import * as React from "react";
import { AlertTriangle, Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import type { ClusterMember, ProjectListResponse, StoragePoolInfo } from "@/types/api";
import { emptyMigrationMappings } from "@/types/migrations";
import { LXD_NAME_PATTERN, isSameLocation, type MigrationDraftState } from "./migration-wizard-model";

const NEW_PROJECT = "__new__";
const AUTO_MEMBER = "__auto__";
const DEFAULT_POOL = "__default__";
const NAME_RULE = "Use letters, digits, and dashes, starting with a letter or digit, up to 63 characters.";

export function MigrationDestinationForm({
  draft,
  onChange,
  clusterOptions,
  projects,
  targetMembers,
  targetPools,
  inventoryLoading,
  resourceLoading,
  inventoryError,
}: {
  draft: MigrationDraftState;
  onChange: (patch: Partial<MigrationDraftState>) => void;
  clusterOptions: SearchableSelectOption[];
  projects: ProjectListResponse[];
  targetMembers: ClusterMember[];
  targetPools: StoragePoolInfo[];
  inventoryLoading: boolean;
  resourceLoading: boolean;
  inventoryError?: string | null;
}) {
  const existingProjects = projects
    .filter((entry) => entry.host === draft.targetCluster && entry.reachable !== false)
    .flatMap((entry) => entry.projects.map((project) => project.name));
  const known = existingProjects.includes(draft.targetProject);
  const [creating, setCreating] = React.useState(() => !!draft.targetProject && !known);
  React.useEffect(() => {
    if (known) setCreating(false);
  }, [known]);

  const projectOptions: SearchableSelectOption[] = [
    ...existingProjects.map((name) => ({
      value: name,
      label: name,
      sublabel: name === draft.sourceProject && draft.targetCluster === draft.sourceCluster ? "Source project" : undefined,
    })),
    { value: NEW_PROJECT, label: "New Project", sublabel: "Created with the source project's settings" },
  ];
  const sameLocation = isSameLocation(draft);
  const projectNameValid = LXD_NAME_PATTERN.test(draft.targetProject);
  const resourceNameValid = draft.scope === "project" || LXD_NAME_PATTERN.test(draft.targetResourceName);
  const showMembers = targetMembers.length > 1;
  const memberOptions = [
    { value: AUTO_MEMBER, label: "Automatic Placement" },
    ...targetMembers.map((member) => ({
      value: member.server_name,
      label: member.server_name,
      sublabel: `${member.status}${member.architecture ? ` · ${member.architecture}` : ""}${
        member.server_name === draft.sourceMember && draft.targetCluster === draft.sourceCluster ? " · current" : ""
      }`,
      disabled: draft.targetCluster === draft.sourceCluster && member.server_name === draft.sourceMember && draft.targetProject === draft.sourceProject,
    })),
  ];
  const poolOptions = [
    { value: DEFAULT_POOL, label: "Same Pool or Project Default" },
    ...targetPools.map((pool) => ({ value: pool.name, label: pool.name, sublabel: `${pool.driver} · ${pool.status}` })),
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="migration-target-cluster">Destination cluster</Label>
          <SearchableSelect
            id="migration-target-cluster"
            value={draft.targetCluster}
            onValueChange={(targetCluster) => {
              setCreating(false);
              onChange({
                targetCluster,
                targetProject: "",
                targetMember: "",
                targetStoragePool: "",
                mappings: emptyMigrationMappings(),
                acknowledgements: [],
              });
            }}
            options={clusterOptions}
            placeholder={inventoryLoading ? "Loading clusters" : "Choose a destination cluster"}
            searchPlaceholder="Search clusters"
            emptyText="No reachable cluster available"
            disabled={inventoryLoading}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="migration-target-project">Destination project</Label>
          <SearchableSelect
            id="migration-target-project"
            value={creating ? NEW_PROJECT : draft.targetProject}
            onValueChange={(value) => {
              const next = value === NEW_PROJECT;
              setCreating(next);
              onChange({ targetProject: next ? "" : value, mappings: emptyMigrationMappings(), acknowledgements: [] });
            }}
            options={projectOptions}
            placeholder={inventoryLoading ? "Loading projects" : "Choose a project"}
            searchPlaceholder="Search projects"
            emptyText="No project reported on this cluster"
            disabled={!draft.targetCluster || inventoryLoading}
          />
          {creating && (
            <div className="space-y-1">
              <Label htmlFor="migration-target-project-new" className="text-xs">
                New project name
              </Label>
              <Input
                id="migration-target-project-new"
                value={draft.targetProject}
                onChange={(event) => onChange({ targetProject: event.target.value, acknowledgements: [] })}
                autoComplete="off"
                aria-invalid={draft.targetProject.length > 0 && !projectNameValid}
              />
              {draft.targetProject.length > 0 && !projectNameValid && (
                <p className="text-xs text-destructive">{NAME_RULE}</p>
              )}
            </div>
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
            <p className="text-xs text-destructive">{NAME_RULE}</p>
          )}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        {showMembers && (
          <div className="space-y-2">
            <Label htmlFor="migration-target-member">Destination member (optional)</Label>
            <SearchableSelect
              id="migration-target-member"
              value={draft.targetMember || AUTO_MEMBER}
              onValueChange={(value) => onChange({ targetMember: value === AUTO_MEMBER ? "" : value })}
              options={memberOptions}
              placeholder="Let the cluster place it"
              searchPlaceholder="Search members"
              emptyText="No member reported"
              disabled={!draft.targetCluster || resourceLoading}
            />
            {draft.intent === "move" && draft.targetCluster === draft.sourceCluster && draft.targetProject === draft.sourceProject && (
              <p className="text-xs text-muted-foreground">
                Same project and name with another member relocates the instance itself.
              </p>
            )}
          </div>
        )}
        <div className="space-y-2">
          <Label htmlFor="migration-target-pool">Destination storage pool (optional)</Label>
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
        </div>
      </div>

      {sameLocation && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" aria-hidden />
          <AlertTitle>Source and destination are identical</AlertTitle>
          <AlertDescription>Choose another cluster, project, member, or instance name.</AlertDescription>
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
        <AlertTitle>Preflight checks the destination</AlertTitle>
        <AlertDescription>
          Ownership, daemon version, capacity, quotas, project restrictions, networks, and name
          conflicts are verified by the server before anything changes.
        </AlertDescription>
      </Alert>
    </div>
  );
}
