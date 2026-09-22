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
import { ProjectCard } from "./project-card";

interface ProjectsSectionProps {
  projectsData: ProjectListResponse[] | undefined;
  projectsLoading: boolean;
  /** The query's own failure, distinct from a fleet that genuinely holds no projects. */
  projectsError: unknown;
  vmsData: VMInfo[];
  refetch: () => void;
  lastUpdatedAt: number | null;
  /** The projects query's dataUpdatedAt, what the post-create focus trusts. */
  projectsUpdatedAt: number;
}

interface ProjectsContext {
  /** Instance names and tags for a project, indexed once instead of re-scanned per call. */
  index: Map<string, { names: string[]; tags: string[]; count: number }>;
}

// A card grid, so it takes the shared toolbar and filter engine but none of the table
// plumbing. Tags stay behind TagFilter rather than the generic facet popover: getTagColor
// derives a tag's colour from its prefix, so the colour is data here, not decoration.
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
      // Tags facet already read, which is why this is the context-aware `filter` spelling rather
      // than fromSort: a sort accessor is handed the row and nothing else, so there is no sort
      // accessor here for a filter to be derived from. The sort side is merged in inside the
      // component, where the index is in scope.
      //
      // note: declared rather than left as a sort-only dimension. check-table-filters.mjs bans a
      // column an operator can order by but not filter by, and a locally merged accessor would
      // slip past that rule only because the parser cannot see where the accessor was put.
      // The None preset also answers "which projects are empty", which nothing else here does.
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

function ProjectsSectionContent({ projectsData, projectsLoading, projectsError, vmsData, refetch, lastUpdatedAt, projectsUpdatedAt }: ProjectsSectionProps) {
  const lastUpdated = useRelativeTime(lastUpdatedAt);
  const headingId = React.useId();
  const [expandedProjects, setExpandedProjects] = React.useState<Set<string>>(new Set());
  // important: the page inside each expanded card lives here, beside expandedProjects, rather
  // than inside ProjectCard. The grid pager below slices the card list, so a card off the current
  // page unmounts and loses any state it held, while this Set survives. A child-held page would
  // therefore bring the card back looking restored and showing different rows, which is worse
  // than either losing the state cleanly or keeping it.
  const [instancePages, setInstancePages] = React.useState<Map<string, number>>(new Map());

  // note: page and size stay in component state and out of the URL. lib/table-filter.ts records
  // that decision for the whole console, and a page number is not a stable thing to share: it
  // resolves against the reader's page size, sort and today's fleet, not the writer's.
  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState(12);
  const resetPage = React.useCallback(() => setPage(1), []);
  // note: one index built once, instead of getProjectInstances re-scanning the whole
  // instance list inside a predicate that ran from both a reduce and a map on every render.
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

  // The instance count is not a field on a project: it lives in the index built from the
  // instances query, and a declared sort accessor is handed the row and nothing else. So this one
  // is merged in here, where the index is in scope, while the rest still come from PROJECTS so
  // the control and the table cannot drift apart.
  //
  // important: keyed off projectIndex, which is memoised on vmsData. The old projectInstanceCounts
  // prop computed the identical number but was rebuilt as a fresh Map on every Dashboard render,
  // so closing over it would hand sortItems a new identity every render and re-sort the whole
  // list each time.
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

  // Get unique nodes from projects
  // Collect all unique tags from projects and instances
  const allTags = React.useMemo(() => {
    const tags = new Set<string>();
    
    // Tags from projects - handle null/undefined projectsData
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
    
    // Tags from instances
    vmsData.forEach((vm) => {
      vm.tags?.forEach((tag) => tags.add(tag));
    });
    
    return Array.from(tags).sort();
  }, [projectsData, vmsData]);

  const toggleProject = (projectKey: string) => {
    const newExpanded = new Set(expandedProjects);
    if (newExpanded.has(projectKey)) {
      newExpanded.delete(projectKey);
      // Collapsing is the operator putting the card away, so reopening starts at the top. Done at
      // the source of the change rather than from an effect watching isExpanded. Dropping the
      // entry rather than writing 1 also keeps the map to the cards that are actually open.
      setInstancePages((pages) => {
        if (!pages.has(projectKey)) return pages;
        const next = new Map(pages);
        next.delete(projectKey);
        return next;
      });
    } else {
      newExpanded.add(projectKey);
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

  // note: this used to union the index hits back on top of filteredRows. Those extras never went
  // through the facet masks, so a project the Node or Tags facet excluded still rendered a card,
  // and the toolbar counted filteredRows while the grid showed more. The search now reaches the
  // index through the hook's extraSearch, so one set feeds both.
  //
  // note: `reachable` is optional on the wire, so only an explicit false counts as unreachable.
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
  // Every host answered and every one of them failed: the fleet is not empty, it is unreadable.
  const allNodesUnreachable =
    (projectsData?.length ?? 0) > 0 && unreachableNodes.length === projectsData!.length;

  // Filter, then sort, then slice, in that order, which is the contract use-table-filters.ts
  // states: the sort covers the whole filtered set rather than the page an operator happens to be
  // looking at. Before this the grid rendered filteredRows unsorted, so the order was whatever the
  // per-node fan-out returned and a card could move between pages on any refetch.
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
  // keyed on the row count would snap an operator back a page mid-task on any refresh that shrank
  // the list, and would still paint one wrong frame before it caught up. Math.max(1, ...) rather
  // than a bare Math.ceil because an empty result gives 0 pages, and Math.min would then hand the
  // slice a page zero.
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

      {/* The sort control rides the toolbar's actions slot rather than taking a row of its own.
          A card grid has no column headers to click, so this is the only place the order can be
          read or changed, and it belongs beside the other controls rather than above a grid that
          already carries a filter row and a tag row. */}
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

      {/* Tag Filter: an alternative renderer for the same facet, not a second control. Its
          colours come from the tag prefix, which the generic popover cannot show. */}
      {allTags.length > 0 && (
        <TagFilter
          availableTags={allTags}
          selectedTags={selectedTags}
          onTagsChange={(tags) => filters.setFacet("tags", tags)}
        />
      )}
      
      {/* A host that did not answer is not a host with no projects. The payload has carried
          `reachable` and a per-host `error` all along; this section was the one place that
          dropped both and drew an empty grid instead. Same component the other fan-out lists
          use, so the wording and the role gate on the reason text stay in one place. */}
      <UnreachableNodesBanner
        noun="projects"
        nodes={unreachableNodes}
        reasons={unreachableReasons}
      />

      {/* important: two-up starts at lg and three-up at xl, not md and lg. Measured live, the
          old thresholds gave a 224px card at both 768 and 1024, which leaves the header's title
          column about 52px once the host badge and the kebab have taken their fixed width, so a
          name as short as "Ryo-Labs" broke mid-word. The card needs roughly 340px to hold its own
          header. At 1280 a three-up card is 309px, which clears it, so the widest band is
          unchanged. */}
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
                  const instanceCount = projectIndex.get(projectKey)?.count ?? 0;
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
                // important: three different situations used to share one sentence. "No projects
                // yet" is a claim about the fleet, and it was printed just as readily when the
                // read had failed or when every node was unreachable, which tells the operator
                // the opposite of what happened. The payload already distinguishes them.
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
                      title="No projects yet"
                      description="Projects appear here once a cluster reports one."
                    />
                  )}
                </div>
              ))}
          </>
        )}
      </div>

      {/* note: 12, 24, 48 and 96 rather than the shared default of 10, 25, 50, 100, 200. The grid
          is one, two or three columns wide and every one of these divides by all three, so a full
          page never leaves a ragged last row.
          No mt-4 wrapper, unlike the table pages: those follow a Table inside a CardContent with
          no vertical rhythm of its own, while the parent here is already space-y-4. */}
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
