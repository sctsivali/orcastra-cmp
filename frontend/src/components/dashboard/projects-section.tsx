"use client";

import * as React from "react";
import { FolderKanban, AlertTriangle } from "lucide-react";
import { LoadingLottieMediumEnhanced } from "@/components/loading-lottie";
import { useRelativeTime } from "@/hooks/use-relative-time";
import { TagFilter } from "@/components/tag-manager";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { useTableSort } from "@/hooks/use-table-sort";
import { defineTable, fromSort } from "@/lib/table-filter";
import { SortSelect } from "@/components/ui/sort-select";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { VMInfo, ProjectListResponse, ProjectInfo } from "@/types";
import { useRowFocus } from "@/hooks/use-row-focus";
import { projectRowKey } from "@/lib/row-focus";
import { RowFocusNotice } from "@/components/ui/row-focus-notice";
import { ProjectsIllustration } from "@/components/illustrations/scenes/platform";
import { INSTANCE_PAGE_SIZE, ProjectCard } from "./project-card";

interface ProjectsSectionProps {
  projectsData: ProjectListResponse[] | undefined;
  projectsLoading: boolean;
  /** The query's own failure, distinct from a fleet that holds no projects. */
  projectsError: unknown;
  vmsData: VMInfo[];
  /** The instance list has not answered yet: project cards show their count as loading. */
  instancesPending?: boolean;
  /**
   * Clusters the instance list could not read (its unreachable_nodes), with the reasons when the
   * server gave them. A project on one of them has an unknown count, not zero.
   */
  instancesUnread?: string[];
  instancesUnreadReasons?: Record<string, string>;
  refetch: () => void;
  lastUpdatedAt: number | null;
  /** The projects query's dataUpdatedAt, what the post-create focus trusts. */
  projectsUpdatedAt: number;
}

interface ProjectsContext {
  /** Instance names and tags for a project, indexed once instead of re-scanned per call. */
  index: Map<string, { names: string[]; tags: string[]; count: number }>;
}

// A card grid: the shared toolbar and filter engine without the table plumbing. Tags render
// through TagFilter rather than the generic facet popover, because getTagColor derives a tag's
// colour from its prefix and the popover cannot show it.
const PROJECTS = defineTable<ProjectInfo, ProjectsContext>()({
  searchPlaceholder: "Search projects by name, cluster, description, or instance",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Project", sort: (p) => p.name, search: (p) => p.name, filter: fromSort("text") },
    {
      field: "host",
      label: "Cluster",
      sort: (p) => p.host,
      search: (p) => p.host,
      facet: (p) => p.host,
      facetSpec: { role: "node", primary: true } },
    { field: "description", label: "Description", sort: (p) => p.description, search: (p) => p.description, filter: fromSort("text") },
    {
      field: "instances",
      label: "Instances",
      // The count is not a field on a project. It comes off the same index the search and the
      // Tags facet read, so this uses the context-aware `filter` form; fromSort needs a sort
      // accessor, which is handed the row alone. The sort side is merged in inside the component.
      //
      // Declared here so the sortable column carries a filter, as check-table-filters.mjs asks;
      // the None preset also answers which projects are empty.
      filter: {
        kind: "number",
        value: (p, ctx) => ctx.index.get(`${p.host}:${p.name}`)?.count ?? 0,
        unit: "count",
        presets: [
          { label: "None", min: null, max: 0 },
          { label: "Any", min: 1, max: null },
        ] } },
    {
      field: "tags",
      label: "Tags",
      facet: (p, ctx) => {
        const entry = ctx.index.get(`${p.host}:${p.name}`);
        return Array.from(new Set([...(p.tags ?? []), ...(entry?.tags ?? [])]));
      },
      facetSpec: { emptyLabel: "(untagged)" } },
  ] });

// The card grid has no column headers to click, so the sort dimension needs a control of its
// own. Three of the five columns: Description and Tags are not orders anyone asks a project list
// for. The labels are read off the table so this control and the toolbar cannot end up naming one
// dimension two different ways.
type ProjectSortField = "name" | "host" | "instances";

const PROJECT_SORT_OPTIONS: readonly { value: ProjectSortField; label: string }[] = [
  { value: "name", label: PROJECTS.byField.name.label },
  { value: "host", label: PROJECTS.byField.host.label },
  { value: "instances", label: PROJECTS.byField.instances.label },
];

// The boundary lives here, next to the useTableFilters read that needs it, so the section can
// be mounted from any page without that page having to know why.
export function ProjectsSection(props: ProjectsSectionProps) {
  return (
    <React.Suspense fallback={<LoadingLottieMediumEnhanced showProgress={false} />}>
      <ProjectsSectionContent {...props} />
    </React.Suspense>
  );
}

function ProjectsSectionContent({ projectsData, projectsLoading, projectsError, vmsData, instancesPending = false, instancesUnread, instancesUnreadReasons, refetch, lastUpdatedAt, projectsUpdatedAt }: ProjectsSectionProps) {
  const lastUpdated = useRelativeTime(lastUpdatedAt);
  const headingId = React.useId();
  const [expandedProjects, setExpandedProjects] = React.useState<Set<string>>(new Set());
  // important: the page inside each expanded card lives here, beside expandedProjects. The grid
  // pager unmounts cards off the current page while this Set survives, so a page held in the
  // card would reopen it on different rows.
  const [instancePages, setInstancePages] = React.useState<Map<string, number>>(new Map());

  // Page and size stay in component state, out of the URL, as lib/table-filter.ts sets for the
  // whole console: a shared page number would resolve against the reader's page size, sort and
  // fleet.
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(12);
  const resetPage = React.useCallback(() => setPage(1), []);
  // One index per instance list, so search, facets and counts never re-scan the whole list.
  const projectIndex = React.useMemo(() => {
    const index = new Map<string, { names: string[]; tags: string[]; count: number }>();
    for (const vm of (vmsData ?? []) as any[]) {
      const key = `${vm.host}:${vm.project || "default"}`;
      const entry = index.get(key) ?? { names: [], tags: [], count: 0 };
      entry.names.push(vm.name);
      if (vm.tags?.length) entry.tags.push(...vm.tags);
      entry.count += 1;
      index.set(key, entry);
    }
    return index;
  }, [vmsData]);

  const allProjects = React.useMemo(
    () => (projectsData ?? []).flatMap((n: any) => n.projects as ProjectInfo[]),
    [projectsData],
  );

  const filterContext = React.useMemo(() => ({ index: projectIndex }), [projectIndex]);

  // The declared accessors see the project row only. Instance names, their tags and the instance
  // count live in the index, so the search reaches them from here and then passes through every
  // active facet like any other hit.
  const searchProjectIndex = React.useCallback(
    (project: ProjectInfo, query: string) => {
      const entry = projectIndex.get(`${project.host}:${project.name}`);
      if (!entry) return false;
      return (
        entry.names.some((n) => n.toLowerCase().includes(query)) ||
        entry.tags.some((t) => t.toLowerCase().includes(query)) ||
        String(entry.count).includes(query)
      );
    },
    [projectIndex],
  );

  const filters = useTableFilters(PROJECTS, allProjects, {
    context: filterContext,
    extraSearch: searchProjectIndex,
    // One line covers every way the result set can change: the toolbar search, the Node facet,
    // the Instances range, chip removal, Clear all, and the separate TagFilter below, which
    // reaches setFacet and therefore the same callback.
    onFilterChange: resetPage,
    // Tags render through TagFilter rather than the toolbar, so keep the facet out of the
    // toolbar's own controls. It still applies, and it still shows as a removable chip.
    availableFields: PROJECTS.facetFields.filter((f) => f !== "tags") });
  const selectedTags = filters.selectedValues("tags");
  const { isReadOnly } = useReadOnlyMode();

  // The instance count lives in the index, and a declared sort accessor is handed the row alone,
  // so its accessor is merged in here; the rest come from PROJECTS so the control and the table
  // cannot drift apart.
  //
  // important: keyed off projectIndex, which is memoised on vmsData. A dependency rebuilt on every
  // render would hand sortItems a new identity and re-sort the whole list each time.
  const sortAccessors = React.useMemo(
    () => ({
      ...PROJECTS.sortAccessors,
      instances: (p: ProjectInfo) => projectIndex.get(`${p.host}:${p.name}`)?.count ?? 0 }),
    [projectIndex],
  );

  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    PROJECTS.defaultSort.field,
    PROJECTS.defaultSort.direction,
    resetPage,
  );

  // Every tag on a project or an instance, for TagFilter.
  const allTags = React.useMemo(() => {
    const tags = new Set<string>();
    
    if (projectsData && Array.isArray(projectsData)) {
      projectsData.forEach((nodeProjects) => {
        if (nodeProjects && nodeProjects.projects) {
          nodeProjects.projects.forEach((project) => {
            if (project.tags && Array.isArray(project.tags)) {
              project.tags.forEach((tag) => tags.add(tag));
            }
          });
        }
      });
    }
    
    vmsData.forEach((vm) => {
      vm.tags?.forEach((tag) => tags.add(tag));
    });
    
    return Array.from(tags).sort();
  }, [projectsData, vmsData]);

  const toggleProject = (projectKey: string) => {
    const newExpanded = new Set(expandedProjects);
    if (newExpanded.has(projectKey)) {
      newExpanded.delete(projectKey);
      // Collapsing resets the card to its first page, here at the source rather than in an effect
      // watching isExpanded. Deleting the entry keeps the map to the cards that are open.
      setInstancePages((pages) => {
        if (!pages.has(projectKey)) return pages;
        const next = new Map(pages);
        next.delete(projectKey);
        return next;
      });
    } else {
      newExpanded.add(projectKey);
      // A search that matched an instance name opens the card on the page holding the first
      // match; the card filters none of its instances, so the match could sit on a later page.
      const query = filters.searchInput.trim().toLowerCase();
      const match = query
        ? vmsData
            .filter((vm) => projectRowKey(vm.host, vm.project || "default") === projectKey)
            .findIndex((vm) => vm.name.toLowerCase().includes(query))
        : -1;
      if (match >= 0) {
        const holding = Math.floor(match / INSTANCE_PAGE_SIZE) + 1;
        setInstancePages((pages) => new Map(pages).set(projectKey, holding));
      }
    }
    setExpandedProjects(newExpanded);
  };

  const setInstancePage = React.useCallback((projectKey: string, nextPage: number) => {
    setInstancePages((pages) => {
      const next = new Map(pages);
      next.set(projectKey, nextPage);
      return next;
    });
  }, []);

  const getProjectInstances = (host: string, projectName: string) => {
    return vmsData.filter(vm => 
      vm.host === host && (vm.project || "default") === projectName
    );
  };

  const getProjectStats = (host: string, projectName: string) => {
    const instances = getProjectInstances(host, projectName);
    const running = instances.filter(vm => vm.status === "Running").length;
    const stopped = instances.filter(vm => vm.status === "Stopped").length;
    const other = instances.length - running - stopped;
    return { running, stopped, other, total: instances.length };
  };

  // `reachable` is optional on the wire, so only an explicit false counts as unreachable.
  // Treating undefined as unreachable would light the banner for every older payload.
  const unreachableNodes = React.useMemo(
    () => (projectsData ?? []).filter((n) => n?.reachable === false).map((n) => n.host),
    [projectsData],
  );
  const unreachableReasons = React.useMemo(() => {
    const out: Record<string, string> = {};
    for (const n of projectsData ?? []) {
      if (n?.reachable === false && n.error) out[n.host] = n.error;
    }
    return out;
  }, [projectsData]);
  // A cluster whose projects answered while its instance list did not (one past the inventory
  // wall): its cards are on the grid with an unknown count, so it is read in part. A cluster
  // missing from both is already counted as unreachable, and counting it twice says nothing new.
  const unreadInstanceHosts = React.useMemo(() => {
    const projectsMissing = new Set(unreachableNodes);
    return (instancesUnread ?? []).filter((host) => !projectsMissing.has(host));
  }, [instancesUnread, unreachableNodes]);
  // The instance list's reasons go under the projects', so an unreachable host keeps the
  // reason its projects read gave. The two sets are disjoint above, so nothing else is shadowed.
  const reachReasons = React.useMemo(
    () => ({ ...(instancesUnreadReasons ?? {}), ...unreachableReasons }),
    [instancesUnreadReasons, unreachableReasons],
  );
  const unreadHostSet = React.useMemo(() => new Set(instancesUnread ?? []), [instancesUnread]);
  // Every host reported a failure, so the fleet is unreadable rather than empty.
  const allNodesUnreachable =
    (projectsData?.length ?? 0) > 0 && unreachableNodes.length === projectsData!.length;

  // Filter, then sort, then slice, the order use-table-filters.ts requires: the sort covers the
  // whole filtered set, so a card does not move between pages on a refetch.
  const sortedProjects = React.useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );
  const projectKeyOf = React.useCallback((p: ProjectInfo) => projectRowKey(p.host, p.name), []);
  const focus = useRowFocus({
    list: "projects",
    rows: allProjects,
    orderedRows: sortedProjects,
    keyOf: projectKeyOf,
    pageSize,
    setPage,
    dataUpdatedAt: projectsUpdatedAt,
    clearFilters: filters.clearAll,
  });

  // important: clamped on read, never corrected in an effect. useAllProjects polls, and an effect
  // keyed on the row count would move an operator back a page on any refresh that shrank the
  // list, after one wrong frame. Math.max(1, ...) because an empty result gives 0 pages and the
  // slice would get page zero.
  const totalPages = Math.max(1, Math.ceil(sortedProjects.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const visibleProjects = sortedProjects.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  return (
    // A landmark named by its heading: the Clusters grid above carries the same toolbar shape
    // (a "Sort by" control, a status counter, a Clear search button), and the region is what
    // lets a screen reader and a locator tell the two apart.
    <section aria-labelledby={headingId} data-slot="projects-section" className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h2 id={headingId} className="text-lg font-semibold">Projects</h2>
          {!!lastUpdatedAt && (
            <p className="text-xs text-muted-foreground">
              Updated {lastUpdated}
            </p>
          )}
        </div>
      </div>

      {/* The sort control sits in the toolbar's actions slot: with no column headers to click it
          is the only place to read or change the order, and the grid already carries a filter
          row and a tag row. */}
      <TableToolbar
        filters={filters}
        noun="projects"
        actions={
          <SortSelect
            options={PROJECT_SORT_OPTIONS}
            value={sortField as ProjectSortField}
            direction={sortDirection}
            onSort={handleSort}
          />
        }
      />

      {/* Another renderer for the `tags` facet, with colours from the tag prefix. */}
      {allTags.length > 0 && (
        <TagFilter
          availableTags={allTags}
          selectedTags={selectedTags}
          onTagsChange={(tags) => filters.setFacet("tags", tags)}
        />
      )}
      
      {/* A host that did not answer is named with its per-host `error`, through the component
          the other fan-out lists use, so the wording and the role gate on the reason stay in one
          place. One banner covers both reads, so it gives one cluster total. Instance-only hosts
          join once the instance list has answered: until then a count is loading, not missing. */}
      <UnreachableNodesBanner
        noun="projects and instances"
        partialNoun="instances"
        nodes={unreachableNodes}
        partialNodes={instancesPending ? undefined : unreadInstanceHosts}
        reasons={reachReasons}
      />

      {/* Two-up starts at lg and three-up at xl: the card header needs about 340px. One breakpoint
          earlier, a card at a 768px or 1024px viewport is 224px wide, leaving the title about
          52px beside the host badge and the menu. */}
      {focus.hidden && (
        <RowFocusNotice label={focus.hidden.label} onShow={focus.showHidden} className="mb-4" />
      )}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-4">
        {projectsLoading ? (
          <div className="col-span-full flex justify-center py-8">
            <LoadingLottieMediumEnhanced showProgress={false} />
          </div>
        ) : (
          <>
            {visibleProjects.map((project) => {
                  const projectKey = projectRowKey(project.host, project.name);
                  const instanceCount =
                    instancesPending || unreadHostSet.has(project.host)
                      ? null
                      : projectIndex.get(projectKey)?.count ?? 0;
                  const isExpanded = expandedProjects.has(projectKey);
                  const instances = getProjectInstances(project.host, project.name);
                  const stats = getProjectStats(project.host, project.name);

                  return (
                    <ProjectCard
                      key={projectKey}
                      project={project}
                      projectKey={projectKey}
                      rowProps={focus.rowProps(projectKey)}
                      instanceCount={instanceCount}
                      countPending={instancesPending}
                      isExpanded={isExpanded}
                      instances={instances}
                      stats={stats}
                      toggleProject={toggleProject}
                      instancePage={instancePages.get(projectKey) ?? 1}
                      onInstancePageChange={setInstancePage}
                      refetch={refetch}
                      isReadOnly={isReadOnly}
                    />
                  );
                })}
            {sortedProjects.length === 0 &&
              (filters.activeFilterCount > 0 ? (
                <div className="col-span-full">
                  <EmptyState
                    icon={FolderKanban}
                    title="No projects found"
                    description="Try a different search, or clear a filter."
                  />
                </div>
              ) : (
                // Three situations, three sentences: "No projects yet" is a claim about the fleet
                // and must not show when the read failed or every cluster was unreachable.
                <div className="col-span-full">
                  {projectsError ? (
                    <EmptyState
                      icon={AlertTriangle}
                      title="Could not load projects"
                      description="The request for the project list did not complete. Refresh to try again."
                    />
                  ) : allNodesUnreachable ? (
                    <EmptyState
                      icon={AlertTriangle}
                      title="No cluster answered"
                      description="Every cluster in scope failed to report its projects, so this list is unknown rather than empty."
                    />
                  ) : (
                    <EmptyState
                      icon={FolderKanban}
                      illustration={ProjectsIllustration}
                      title="No projects yet"
                      description="Projects appear here once a cluster reports one."
                    />
                  )}
                </div>
              ))}
          </>
        )}
      </div>

      {/* 12, 24, 48 and 96 instead of the shared default of 10, 25, 50, 100, 200: each divides by
          one, two and three columns, so a full page never leaves a ragged last row. No mt-4
          wrapper, unlike the table pages: the parent here is already space-y-4. */}
      {totalPages > 1 && (
        <div data-slot="projects-pagination">
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            pageSize={pageSize}
            totalItems={sortedProjects.length}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(1);
            }}
            pageSizeOptions={[12, 24, 48, 96]}
          />
        </div>
      )}
    </section>
  );
}
