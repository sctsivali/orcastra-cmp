"use client";

import * as React from "react";
import { parseLxdSize } from "@/lib/lxd-limits";
import {
  FolderKanban,
  ChevronDown,
  ChevronUp,
  Trash2,
  Cpu,
  HardDrive,
  MemoryStick,
  Settings,
  ArrowLeftRight,
  Boxes,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { TagManager } from "@/components/tag-manager";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { TableEmptyValue } from "@/components/ui/table-text";
import { RowActions, type RowAction } from "@/components/ui/row-actions";
import { useUpdateProjectTags } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { toast } from "@/hooks/use-toast";
import { VMInfo, ProjectInfo } from "@/types";
import { OverflowText } from "@/components/ui/overflow-text";
import { InstanceRow } from "./instance-row";
import { ProjectDeleteDialog, ProjectEditDialog } from "./project-card-dialogs";
import { NodeLink } from "@/components/ui/node-link";
import { formatBytes } from "@/lib/utils";
import { inventoryLinks } from "@/lib/inventory-links";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useMigrationCapabilities } from "@/hooks/use-read-only-mode";

interface ProjectCardProps {
  project: ProjectInfo;
  projectKey: string;
  /** From useRowFocus: the row key and, briefly after a create, the mark. */
  rowProps: { "data-row-key": string; "data-row-focus"?: "true" | "false" };
  /**
   * How many instances the project holds, or null when that is not known: the instance list is
   * still loading (countPending), or this project's cluster did not answer it. Never 0 for
   * unknown, because 0 enables Delete and reads as an empty project.
   */
  instanceCount: number | null;
  countPending?: boolean;
  isExpanded: boolean;
  instances: VMInfo[];
  stats: { running: number; stopped: number; other: number; total: number };
  toggleProject: (key: string) => void;
  /**
   * The page of the expanded instance list.
   *
   * important: owned by ProjectsSection. The grid pager unmounts cards off the current page
   * while the expanded set survives, so a page held in the card would reopen it on other rows.
   */
  instancePage: number;
  onInstancePageChange: (key: string, page: number) => void;
  refetch: () => void;
  isReadOnly?: boolean;
}

// One page of instances inside an expanded project card. Five, the same number the app's other
// in-card pager uses, so the two compact pagers do not disagree about what a page is.
export const INSTANCE_PAGE_SIZE = 5;

export function ProjectCard({ project, projectKey, rowProps, instanceCount, countPending = false, isExpanded, instances, stats, toggleProject, instancePage, onInstancePageChange, refetch, isReadOnly = false }: ProjectCardProps) {
  const count = instanceCount ?? 0;
  const updateProjectTags = useUpdateProjectTags();
  const [deleteDialogOpen, setDeleteDialogOpen] = React.useState(false);
  const [editDialogOpen, setEditDialogOpen] = React.useState(false);
  const { isPartner } = usePermissions();
  const { navigate } = useNavigationProgress();
  const migrationCapabilities = useMigrationCapabilities();
  const relocationReason = (enabled: boolean | undefined, label: string) =>
    migrationCapabilities.planningUnavailableReason ??
    (enabled === true ? undefined : `${label} is turned off on this deployment`);
  const openRelocation = (mode: "member" | "cluster") =>
    navigate(
      inventoryLinks.migrations({
        scope: "project",
        sourceCluster: project.host,
        sourceProject: project.name,
        mode,
      }),
    );
  // Planning stays offered in read-only rollout because the backend allows it. A relocation
  // this deployment cannot run is listed with its reason rather than left out.
  const projectActions: RowAction[] = [
    ...(isReadOnly
      ? []
      : [{ id: "edit", label: "Edit Project", icon: Settings, onSelect: () => setEditDialogOpen(true) }]),
    {
      id: "relocate-workloads",
      label: "Relocate Workloads",
      icon: Boxes,
      onSelect: () => openRelocation("member"),
      unavailableReason: relocationReason(migrationCapabilities.projectMemberRelocationEnabled, "Relocate Workloads"),
      separatorBefore: !isReadOnly,
    },
    {
      id: "relocate-project",
      label: "Relocate Project",
      icon: ArrowLeftRight,
      onSelect: () => openRelocation("cluster"),
      unavailableReason: relocationReason(migrationCapabilities.projectCrossClusterEnabled, "Relocate Project"),
    },
    ...(isReadOnly
      ? []
      : [{
          id: "delete",
          label: "Delete Project",
          icon: Trash2,
          destructive: true,
          separatorBefore: true,
          onSelect: () => setDeleteDialogOpen(true),
        }]),
  ];
  // useId rather than an id built from projectKey: the key carries a colon and the project name
  // is operator-supplied, so neither is safe to put in the DOM id the disclosure points at.
  const instancesRegionId = React.useId();

  const handleTagsChange = async (newTags: string[]) => {
    if (isReadOnly) {
      toast({
        title: "Read-only mode active",
        description: "Tag updates are disabled in read-only mode.",
        variant: "destructive" });
      return;
    }
    try {
      await updateProjectTags.mutateAsync({
        host: project.host,
        projectName: project.name,
        tags: newTags });
      toast({
        title: "Tags updated",
        description: `Tags for project '${project.name}' have been updated.`,
        variant: "success" });
      refetch();
    } catch (error) {
      toast({
        title: "Could not update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  // Limits only: the instance list carries no per-instance usage, and LXD counts project usage
  // on allocation, so a live "used" figure would answer a different question.
  const limits = React.useMemo(() => {
    const cpuLimit = project.config?.["limits.cpu"] ? parseInt(project.config["limits.cpu"]) : null;
    
    const ramLimitBytes = parseLxdSize(project.config?.["limits.memory"]);
    const storageLimitBytes = parseLxdSize(project.config?.["limits.disk"]);

    return { cpuLimit, ramLimitBytes, storageLimitBytes };
  }, [project.config]);

  const instanceTotalPages = Math.max(1, Math.ceil(instances.length / INSTANCE_PAGE_SIZE));

  // important: clamped on read, never corrected in an effect. getProjectInstances builds a new
  // array on every ProjectsSection render and useInstances polls, so an effect keyed on
  // `instances` would reset every open card twice per tick, and one keyed on the overflow would
  // still paint an empty frame first.
  //
  // The stored page is left alone: a cluster that misses one poll drops its instances until the
  // next, and clamping on read returns the card to where the operator left it.
  const instanceCurrentPage = Math.min(instancePage, instanceTotalPages);
  const visibleInstances = instances.slice(
    (instanceCurrentPage - 1) * INSTANCE_PAGE_SIZE,
    instanceCurrentPage * INSTANCE_PAGE_SIZE,
  );

  return (
    <Card
      data-slot="project-card"
      className="hover:border-primary/50 transition-colors data-[row-focus=true]:border-primary data-[row-focus=true]:bg-primary/10 data-[row-focus]:duration-700 motion-reduce:data-[row-focus]:transition-none"
      {...rowProps}
    >
      <CardHeader className="pb-3 px-3 md:px-6">
        <div className="flex flex-col sm:flex-row items-start sm:justify-between gap-2">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <FolderKanban className="h-4 w-4 text-muted-foreground shrink-0" />
            <CardTitle className="text-base min-w-0 break-words">{project.name}</CardTitle>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* The cluster it runs on, linked through the same component every table cell uses;
                plain text for a Tenant. */}
            <NodeLink clusterId={project.host} className="text-xs shrink-0" />
            {isPartner && (
              <>
                <RowActions
                  actions={projectActions}
                  triggerLabel={`Project actions for ${project.name}`}
                  className="h-7 w-7"
                />
                {!isReadOnly && (
                  <ProjectDeleteDialog
                    project={project}
                    instanceCount={instanceCount}
                    open={deleteDialogOpen}
                    onOpenChange={setDeleteDialogOpen}
                    isReadOnly={isReadOnly}
                  />
                )}
              </>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {project.description && (
          <div className="text-xs text-muted-foreground">
            <OverflowText
              value={project.description}
              label={`description for project ${project.name}`}
              lines={2}
            />
          </div>
        )}

        <div className="pt-1">
          <TagManager
            tags={project.tags || []}
            onTagsChange={handleTagsChange}
            readonly={isReadOnly}
            maxTags={10}
            placeholder="Add project tag…"
          />
        </div>
        
        {/* The chip grid sizes itself from the card, not from a viewport breakpoint: the sidebar
            collapses, so one viewport gives two card widths. auto-fit fits three chips, then two,
            then one as the card narrows. The 104px floor covers 36px of chip chrome (padding,
            icon, gap, border) plus "Unlimited" at 59px in Geist 12px/600. The value wraps instead
            of truncating, so a limit never reads 5 where it is 512. */}
        <div className="grid gap-2 pt-2 grid-cols-[repeat(auto-fit,minmax(104px,1fr))]">
          {[
            {
              key: "cpu",
              Icon: Cpu,
              label: "vCPU",
              spec: limits.cpuLimit
                ? { value: String(limits.cpuLimit), unit: "vCPU limit" }
                : { value: "Unlimited", unit: "vCPU" },
            },
            {
              key: "ram",
              Icon: MemoryStick,
              label: "Memory",
              spec: limits.ramLimitBytes
                ? limitSpec(formatBytes(limits.ramLimitBytes, { split: true }))
                : { value: "Unlimited", unit: "Memory" },
            },
            {
              key: "storage",
              Icon: HardDrive,
              label: "Storage",
              spec: limits.storageLimitBytes
                ? limitSpec(formatBytes(limits.storageLimitBytes, { split: true }))
                : { value: "Unlimited", unit: "Storage" },
            },
          ].map(({ key, Icon, label, spec }) => (
            <div
              key={key}
              className="flex items-center gap-1.5 px-1.5 py-2 rounded-md bg-muted/40 border border-border"
            >
              <Icon className="h-4 w-4 text-foreground/80 shrink-0" />
              <div className="flex flex-col min-w-0 break-words">
                {/* The icon is the only thing naming the resource when a unit occupies the
                    second line, so a screen reader also gets the resource name in words. */}
                {spec.unit !== label && <span className="sr-only">{label}</span>}
                <span className="text-xs font-semibold text-foreground/80">{spec.value}</span>{" "}
                <span className="text-[10px] text-muted-foreground">{spec.unit}</span>
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-2 pt-2 border-t">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Instances</span>
            {/* One real button around the badge and the chevron, so the card opens by keyboard.
                The badge passes asChild so a span carries its styling: Badge renders a div
                (ui/badge.tsx), and a div inside a button is invalid HTML. */}
            {count > 0 ? (
              <button
                type="button"
                onClick={() => toggleProject(projectKey)}
                aria-expanded={isExpanded}
                aria-controls={instancesRegionId}
                className="flex items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Badge asChild variant="default" className="hover:bg-primary/80 transition-colors">
                  <span>{count}</span>
                </Badge>
                <span className="sr-only">
                  {isExpanded ? "Hide" : "Show"} instances in {project.name} on {project.host}
                </span>
                {isExpanded ? (
                  <ChevronUp className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                )}
              </button>
            ) : instanceCount !== null ? (
              <Badge variant="secondary">{count}</Badge>
            ) : countPending ? (
              <>
                <Skeleton className="h-5 w-7 rounded-sm" aria-hidden="true" />
                <span className="sr-only">Loading the instance count</span>
              </>
            ) : (
              <Badge variant="secondary">
                <TableEmptyValue label="Instance count not read: this cluster's instance list did not answer" />
              </Badge>
            )}
          </div>
          {count > 0 && (
            <p className="text-xs text-muted-foreground">
              {[
                `${stats.running} running`,
                `${stats.stopped} stopped`,
                ...(stats.other > 0 ? [`${stats.other} other`] : []),
              ].join(" · ")}
            </p>
          )}
        </div>

        {isExpanded && count > 0 && (
          <div id={instancesRegionId} className="mt-3 pt-3 border-t space-y-2">
            {visibleInstances.map((vm) => (
              <InstanceRow key={vm.name} vm={vm} onTagsUpdate={refetch} isReadOnly={isReadOnly} />
            ))}
            {/* No count sentence beside this pager: nothing filters here, the badge above holds
                the total, and an aria-live region per card would re-announce every card on each
                poll. */}
            {instanceTotalPages > 1 && (
              <DataTablePagination
                variant="compact"
                currentPage={instanceCurrentPage}
                totalPages={instanceTotalPages}
                onPageChange={(nextPage) => onInstancePageChange(projectKey, nextPage)}
                label={`instances in ${project.name} on ${project.host}`}
              />
            )}
          </div>
        )}
      </CardContent>

      <ProjectEditDialog
        project={project}
        open={editDialogOpen}
        onOpenChange={setEditDialogOpen}
        refetch={refetch}
        isReadOnly={isReadOnly}
      />
    </Card>
  );
}

function limitSpec({ value, unit }: { value: string; unit: string }): { value: string; unit: string } {
  return { value, unit: `${unit} limit` };
}
