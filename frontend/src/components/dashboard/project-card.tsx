"use client";

import * as React from "react";
import { parseLimitsCpu, parseLxdSize } from "@/lib/lxd-limits";
import {
  FolderKanban,
  ChevronDown,
  ChevronUp,
  Trash2,
  MoreVertical,
  Cpu,
  HardDrive,
  MemoryStick,
  Settings,
  ArrowLeftRight,
  Boxes } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TagManager } from "@/components/tag-manager";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { useUpdateProjectTags } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { toast } from "@/hooks/use-toast";
import { VMInfo, ProjectInfo } from "@/types";
import { OverflowText } from "@/components/ui/overflow-text";
import { InstanceRow } from "./instance-row";
import { ProjectDeleteDialog, ProjectEditDialog } from "./project-card-dialogs";
import { NodeLink } from "@/components/ui/node-link";
import { formatByteQuota, formatMemoryQuota } from "@/lib/utils";
import { inventoryLinks } from "@/lib/inventory-links";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useMigrationCapabilities } from "@/hooks/use-read-only-mode";

interface ProjectCardProps {
  project: ProjectInfo;
  projectKey: string;
  /** From useRowFocus: the row key and, briefly after a create, the mark. */
  rowProps: { "data-row-key": string; "data-row-focus"?: "true" | "false" };
  instanceCount: number;
  isExpanded: boolean;
  instances: VMInfo[];
  stats: { running: number; stopped: number; other: number; total: number };
  toggleProject: (key: string) => void;
  /**
   * The page of the expanded instance list.
   *
   * important: owned by ProjectsSection, not by this component. The grid pager slices the card
   * list, so a card off the current page unmounts and loses its own state while the expanded set
   * survives, and a card held page would bring the card back open but showing different rows.
   */
  instancePage: number;
  onInstancePageChange: (key: string, page: number) => void;
  refetch: () => void;
  isReadOnly?: boolean;
}

// One page of instances inside an expanded project card. Five, the same number the app's other
// in-card pager uses, so the two compact pagers do not disagree about what a page is.
const INSTANCE_PAGE_SIZE = 5;

export function ProjectCard({ project, projectKey, rowProps, instanceCount, isExpanded, instances, stats, toggleProject, instancePage, onInstancePageChange, refetch, isReadOnly = false }: ProjectCardProps) {
  const updateProjectTags = useUpdateProjectTags();
  const [deleteDialogOpen, setDeleteDialogOpen] = React.useState(false);
  const [editDialogOpen, setEditDialogOpen] = React.useState(false);
  const { isPartner } = usePermissions();
  const { navigate } = useNavigationProgress();
  const migrationCapabilities = useMigrationCapabilities();
  const canPlanMigration = isPartner && migrationCapabilities.centerEnabled === true;
  // useId rather than an id built from projectKey: the key carries a colon and the project name
  // is operator-supplied, so neither is safe to put in the DOM id the disclosure points at.
  const instancesRegionId = React.useId();

  const handleTagsChange = async (newTags: string[]) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Tag updates are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    try {
      await updateProjectTags.mutateAsync({
        host: project.host,
        projectName: project.name,
        tags: newTags });
      toast({
        title: "Tags Updated",
        description: `Tags for project '${project.name}' have been updated.`,
        variant: "success" });
      refetch();
    } catch (error) {
      toast({
        title: "Failed to update tags",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    }
  };

  // calculate resource usage from instances
  const resourceUsage = React.useMemo(() => {
    let cpuUsed = 0;
    let ramUsedBytes = 0;
    let storageUsedBytes = 0;

    instances.forEach((vm) => {
      // count CPU the way the backend does (a range or a set is its size, not 0). The row
      // carries the instance-local config only, so a profile-inherited limit still counts as
      // unset here; the per-node figures come from the API and are exact.
      const vmCpu = vm.config?.["limits.cpu"];
      if (vmCpu) {
        cpuUsed += parseLimitsCpu(vmCpu, 0);
      }

      // Count memory.
      if (vm.resources?.memory_usage) {
        ramUsedBytes += vm.resources.memory_usage;
      }

      // count storage from disk usage
      if (vm.resources?.disk_usage) {
        Object.values(vm.resources.disk_usage).forEach((bytes) => {
          storageUsedBytes += bytes;
        });
      }
    });

    return { cpuUsed, ramUsedBytes, storageUsedBytes };
  }, [instances]);

  // parse project limits
  const limits = React.useMemo(() => {
    const cpuLimit = project.config?.["limits.cpu"] ? parseInt(project.config["limits.cpu"]) : null;
    
    const ramLimitBytes = parseLxdSize(project.config?.["limits.memory"]);
    const storageLimitBytes = parseLxdSize(project.config?.["limits.disk"]);

    return { cpuLimit, ramLimitBytes, storageLimitBytes };
  }, [project.config]);

  const instanceTotalPages = Math.max(1, Math.ceil(instances.length / INSTANCE_PAGE_SIZE));

  // important: clamped on read, never corrected in an effect. getProjectInstances builds a new
  // array on every ProjectsSection render and useInstances polls, so an effect keyed on
  // `instances` would reset every open card twice per tick while the operator was mid-task, and
  // one keyed on the overflow would still paint an empty frame before it caught up.
  //
  // note: the stored page is deliberately left alone. A node that fails to answer drops its
  // instances from the list and they come back on the next poll, so clamping on read returns the
  // card to where the operator left it, where correcting the stored page would strand it lower
  // after one flaky fan-out.
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
            {/* An identifier, not a status: the node it runs on links into Clusters through the
                same component every table cell uses, and reads as text for a Tenant. */}
            <NodeLink clusterId={project.host} className="text-xs shrink-0" />
            {/* Partner planning stays available in read-only rollout; mutations do not. */}
            {isPartner && (!isReadOnly || canPlanMigration) && (
              <>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild onClick={(e) => e.stopPropagation()}>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      aria-label={`Project actions for ${project.name}`}
                    >
                      <MoreVertical className="h-4 w-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                  {!isReadOnly && (
                    <DropdownMenuItem
                      className="cursor-pointer"
                      onSelect={() => setEditDialogOpen(true)}
                    >
                      <Settings className="mr-2 h-4 w-4" />
                      Edit Project
                    </DropdownMenuItem>
                  )}
                  {canPlanMigration && (
                    <>
                      <DropdownMenuItem
                        className="cursor-pointer"
                        onSelect={() =>
                          navigate(
                            inventoryLinks.migrations({
                              scope: "project",
                              sourceCluster: project.host,
                              sourceProject: project.name,
                              mode: "member",
                            }),
                          )
                        }
                      >
                        <Boxes className="mr-2 h-4 w-4" />
                        Relocate Workloads
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        className="cursor-pointer"
                        onSelect={() =>
                          navigate(
                            inventoryLinks.migrations({
                              scope: "project",
                              sourceCluster: project.host,
                              sourceProject: project.name,
                              mode: "cluster",
                            }),
                          )
                        }
                      >
                        <ArrowLeftRight className="mr-2 h-4 w-4" />
                        Relocate Project
                      </DropdownMenuItem>
                    </>
                  )}
                  {!isReadOnly && (
                    <DropdownMenuItem
                      className="text-destructive focus:text-destructive cursor-pointer"
                      onSelect={() => setDeleteDialogOpen(true)}
                    >
                      <Trash2 className="mr-2 h-4 w-4" />
                      Delete Project
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
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

        {/* Tags */}
        <div className="pt-1">
          <TagManager
            tags={project.tags || []}
            onTagsChange={handleTagsChange}
            readonly={isReadOnly}
            maxTags={10}
            placeholder="Add project tag..."
          />
        </div>
        
        {/* Resource Usage */}
        {/* important: the chip grid sizes itself from the card, not from a viewport breakpoint.
            The sidebar collapses, so one viewport gives two different card widths and an `lg:`
            rule would lie about the room available. `auto-fit` measures the real container: three
            chips across a 385px card at 1728, two then one as the card narrows, so a chip is
            never handed less room than its text needs. The 104px floor is measured rather than
            guessed. Chip chrome costs 36px (padding, icon, gap, border) and the widest short
            value, "Unlimited", is 59px in Geist 12px/600, so even the narrowest chip clears it.
            note: the value wraps, it does not truncate. A quota is what an operator acts on, and
            a wrapped "19.9 of 512.0" still says 512 where a clipped one says "19.9 of 5". */}
        <div className="grid gap-2 pt-2 grid-cols-[repeat(auto-fit,minmax(104px,1fr))]">
          {[
            {
              key: "cpu",
              Icon: Cpu,
              label: "vCPU",
              spec: limits.cpuLimit
                ? { value: `${resourceUsage.cpuUsed} of ${limits.cpuLimit}`, unit: "vCPU" }
                : { value: "Unlimited", unit: "vCPU" },
            },
            {
              key: "ram",
              Icon: MemoryStick,
              label: "Memory",
              spec: limits.ramLimitBytes
                ? formatMemoryQuota(resourceUsage.ramUsedBytes, limits.ramLimitBytes)
                : { value: "Unlimited", unit: "Memory" },
            },
            {
              key: "storage",
              Icon: HardDrive,
              label: "Storage",
              spec: limits.storageLimitBytes
                ? formatByteQuota(resourceUsage.storageUsedBytes, limits.storageLimitBytes)
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
                <span className="text-xs font-semibold text-foreground/80">{spec.value}</span>
                <span className="text-[10px] text-muted-foreground">{spec.unit}</span>
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-2 pt-2 border-t">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">Instances</span>
            {/* important: one real button. The count badge carried the onClick and the chevron
                beside it was a bare div, and Badge renders a div (ui/badge.tsx), so neither took
                focus and the only way to open this card was a mouse. Everything inside the
                expanded region, the instance actions and now its pager, was therefore unreachable
                by keyboard. The badge passes asChild so a span carries the chip styling, because
                a div inside a button is invalid per the HTML content model. */}
            {instanceCount > 0 ? (
              <button
                type="button"
                onClick={() => toggleProject(projectKey)}
                aria-expanded={isExpanded}
                aria-controls={instancesRegionId}
                className="flex items-center gap-2 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                <Badge asChild variant="default" className="hover:bg-primary/80 transition-colors">
                  <span>{instanceCount}</span>
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
            ) : (
              <Badge variant="secondary">{instanceCount}</Badge>
            )}
          </div>
          {instanceCount > 0 && (
            <div className="flex items-center gap-2 text-xs">
              <div className="flex items-center gap-1">
                <div className="h-2 w-2 rounded-full bg-status-online" />
                <span className="text-muted-foreground">{stats.running} running</span>
              </div>
              <div className="flex items-center gap-1">
                <div className="h-2 w-2 rounded-full bg-status-unknown" />
                <span className="text-muted-foreground">{stats.stopped} stopped</span>
              </div>
              {stats.other > 0 && (
                <div className="flex items-center gap-1">
                  <div className="h-2 w-2 rounded-full bg-status-pending" />
                  <span className="text-muted-foreground">{stats.other} other</span>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Expanded Instance List */}
        {isExpanded && instanceCount > 0 && (
          <div id={instancesRegionId} className="mt-3 pt-3 border-t space-y-2">
            {visibleInstances.map((vm) => (
              <InstanceRow key={vm.name} vm={vm} onTagsUpdate={refetch} isReadOnly={isReadOnly} />
            ))}
            {/* note: no count sentence beside this pager, unlike the cluster access panel. There
                the denominator moves under a search so the count had to be spoken. Here nothing
                filters, the badge above already carries the total, and one aria-live region per
                card across a dozen cards would re-announce all of them on every poll tick. */}
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
