"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import { formatBytes, formatHypervisorType, plural } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Globe,
  Server,
  Cpu,
  MemoryStick,
  HardDrive,
  Sparkles,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  MapPin,
  Activity,
  Box,
  Building2,
  type LucideIcon,
} from "lucide-react";
import { useRegionsOverview } from "@/hooks/use-lxd";
import { HardwareSummary } from "./regions-hardware-summary";
import { BasemapConfig } from "@/lib/map-basemap";
import { RegionsMapCard } from "./regions-map-card";
import { REGIONS } from "./regions-table-config";
import { RegionSummary, RegionsOverviewResponse } from "@/types/api";
import { RegionsIllustration } from "@/components/illustrations/scenes/onboarding";
import { isWholeCountry, placeDetail } from "@/lib/regions/place";
import { IllustrationFallback } from "@/components/illustrations/illustration-frame";

function InstanceCounts({ region }: { region: RegionSummary }) {
  return (
    <div className="flex items-center gap-4 text-sm">
      <div className="flex items-center gap-1.5">
        <Box className="h-4 w-4 text-foreground" aria-hidden />
        <span>{plural(region.instance_count, "instance")}</span>
      </div>
      <div className="flex items-center gap-1.5">
        <Activity className="h-4 w-4 text-foreground" aria-hidden />
        <span>{region.instance_running} running</span>
      </div>
    </div>
  );
}

function StatTile({
  icon: Icon,
  label,
  value,
  note }: {
  icon: LucideIcon;
  label: string;
  value: React.ReactNode;
  note?: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-muted-foreground" aria-hidden />
          <span className="text-sm text-muted-foreground">{label}</span>
        </div>
        <p className="text-2xl font-bold mt-1">{value}</p>
        {note && <p className="text-xs text-muted-foreground mt-0.5">{note}</p>}
      </CardContent>
    </Card>
  );
}

// The fleet's hardware totals, or null when the caller may not read them. The backend sends
// null rather than 0 to a Tenant (routers/regions.py): hardware inventory is Partner and
// above, the same line GET /nodes draws, and a zero would claim an empty fleet.
function fleetHardware(data: RegionsOverviewResponse) {
  const { total_cpu, total_memory, total_storage, total_gpu } = data;
  if (total_cpu == null || total_memory == null || total_storage == null || total_gpu == null) {
    return null;
  }
  return {
    cpu: total_cpu,
    memory: total_memory,
    storage: total_storage,
    gpu: total_gpu,
    gpuCards: data.total_gpu_cards ?? total_gpu,
  };
}

// Hardware the backend could not read (an offline cluster, a failed read) arrives as null for
// an operator too. Saying so beats the profile of zeros it used to be, which stated a size.
const HARDWARE_NOT_READ = "Hardware not read";
const NOT_READ = "Not read";

function RegionCard({
  region,
  showsHardware,
  isExpanded,
  onToggle,
  onViewOnMap }: {
  region: RegionSummary;
  /** False for a Tenant, who is sent no hardware at all. */
  showsHardware: boolean;
  isExpanded: boolean;
  onToggle: () => void;
  onViewOnMap: () => void;
}) {
  const onlineClusters = region.nodes.filter((n) => n.status === "online").length;
  // Every cluster with no recorded location is grouped into one synthetic region whose fields
  // the backend fills with "XX" and "Unknown" (routers/regions.py). Rendering those verbatim
  // put an "XX" country badge over the line "Unknown, Unknown", which states a place rather
  // than an absence. The map card beside this list already declines to plot the same row and
  // says so in these words.
  const hasCountry = Boolean(region.country_code) && region.country_code.toUpperCase() !== "XX";

  return (
    <Card id={`region-card-${region.region_id}`} className="overflow-hidden transition-all duration-300">
      <Collapsible open={isExpanded} onOpenChange={onToggle}>
        <CollapsibleTrigger asChild>
          <CardHeader className="cursor-pointer hover:bg-muted/50 transition-colors pb-3">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-foreground text-background">
                  {isWholeCountry(region) ? <Globe className="h-5 w-5" aria-hidden /> : <MapPin className="h-5 w-5" aria-hidden />}
                </div>
                <div>
                  <CardTitle className="text-base flex items-center gap-2">
                    {region.region_name}
                    {hasCountry && (
                      <Badge variant="outline" className="text-xs">
                        {region.country_code}
                      </Badge>
                    )}
                  </CardTitle>
                  <CardDescription className="text-xs">
                    {hasCountry ? placeDetail(region) : "No location data"}
                  </CardDescription>
                  {region.facility_name && (
                    <div className="flex items-center gap-1.5 mt-1">
                      <Building2 className="h-3 w-3 text-muted-foreground" />
                      <span className="text-xs text-muted-foreground font-medium">
                        {region.facility_name}
                        {region.facility_provider && `, ${region.facility_provider}`}
                      </span>
                    </div>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <div className="text-right">
                  <p className="text-sm font-medium">
                    {plural(region.node_count, "cluster")}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {onlineClusters} online
                  </p>
                </div>
                {isExpanded ? (
                  <ChevronUp className="h-4 w-4 text-muted-foreground" />
                ) : (
                  <ChevronDown className="h-4 w-4 text-muted-foreground" />
                )}
              </div>
            </div>
            
            {/* Quick stats: hardware for a Partner or Administrator, the caller's own
                instances for a Tenant, who is sent no hardware. */}
            <div className="mt-3 pt-3 border-t">
              {!showsHardware ? (
                <InstanceCounts region={region} />
              ) : region.hardware ? (
                <HardwareSummary hardware={region.hardware} compact />
              ) : (
                <p className="text-xs text-muted-foreground">{HARDWARE_NOT_READ}</p>
              )}
            </div>
          </CardHeader>
        </CollapsibleTrigger>
        
        <CollapsibleContent>
          <CardContent className="pt-0 space-y-4">
            {showsHardware && (
              <>
                <InstanceCounts region={region} />
                {region.hardware && <HardwareSummary hardware={region.hardware} />}
              </>
            )}
            
            {/* Registered clusters */}
            <div className="space-y-2">
              <p className="text-sm font-medium">Clusters in this region:</p>
              <div className="space-y-1">
                {region.nodes.map((node) => (
                  <div
                    key={node.cluster_id}
                    className="flex items-center justify-between p-2 rounded-md bg-muted/50 text-sm"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{node.display_name}</span>
                      <Badge variant={clusterStatusMeta(node.status).tone}>
                        {clusterStatusMeta(node.status).label}
                      </Badge>
                      {node.node_type && (
                        <Badge variant="secondary" className="text-xs">
                          {formatHypervisorType(node.node_type)}
                        </Badge>
                      )}
                    </div>
                    {node.hardware ? (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <span>{plural(node.hardware.cpu_total, "CPU thread")}</span>
                        <span>•</span>
                        <span>{formatBytes(node.hardware.memory_total)}</span>
                        {node.hardware.gpu_total > 0 && (
                          <>
                            <span>•</span>
                            <span>{node.hardware.gpu_total} GPU</span>
                          </>
                        )}
                      </div>
                    ) : (
                      showsHardware && (
                        <span className="text-xs text-muted-foreground">{HARDWARE_NOT_READ}</span>
                      )
                    )}
                  </div>
                ))}
              </div>
            </div>
            
            {/* View on map button */}
            <Button variant="outline" size="sm" className="w-full" onClick={onViewOnMap}>
              <MapPin className="h-4 w-4 mr-2" />
              View on Map
            </Button>
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

function RegionsContent({ basemap }: { basemap: BasemapConfig }) {
  const { data, isLoading, error, refetch, refreshNow, dataUpdatedAt } = useRegionsOverview();
  const [expandedRegions, setExpandedRegions] = React.useState<Set<string>>(new Set());
  const [selectedRegion, setSelectedRegion] = React.useState<string | null>(null);
  const [focusSignal, setFocusSignal] = React.useState(0);

  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(GRID_PAGE_SIZE);
  const resetPage = React.useCallback(() => setPage(1), []);

  const filters = useTableFilters(REGIONS, (data?.regions ?? []) as any[], {
    onFilterChange: resetPage,
  });
  const filteredRegions = filters.filteredRows;
  const fleet = data ? fleetHardware(data) : null;
  // note: the fleet totals sum the clusters whose hardware was read. When none was, they are 0
  // by arithmetic, not by measurement, so the tiles say so rather than show an empty fleet.
  const nothingRead =
    Boolean(data?.regions.length) && (data?.regions ?? []).every((region) => region.hardware === null);
  const runningInstances = (data?.regions ?? []).reduce(
    (sum, region) => sum + region.instance_running,
    0
  );

  // Clamped on read rather than corrected in an effect: an effect would paint one wrong frame
  // first and then snap the operator back a page mid-task.
  const totalPages = Math.max(1, Math.ceil(filteredRegions.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedRegions = filteredRegions.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  );

  // important: the map plots the whole filtered set while the list shows one page, so a marker
  // can point at a card that is not mounted. handleSelectRegion reads the page through this ref
  // instead of taking it as a dependency, because it has to keep the stable identity the marker
  // layer needs (see below).
  const pagingRef = React.useRef({ regions: filteredRegions, pageSize });
  React.useEffect(() => {
    pagingRef.current = { regions: filteredRegions, pageSize };
  }, [filteredRegions, pageSize]);

  // Toggle region expansion
  const toggleRegion = (regionId: string) => {
    setExpandedRegions((prev) => {
      const next = new Set(prev);
      if (next.has(regionId)) {
        next.delete(regionId);
      } else {
        next.add(regionId);
      }
      return next;
    });
  };

  // Select region from map marker - scroll to region card and expand it
  // important: memoised because the map's marker layer keys its rebuild effect on this
  // callback. As a plain function it took a new identity on every render, so selecting a
  // region re-rendered, tore the whole cluster layer down and rebuilt it, and destroyed the
  // popup the click had just opened. The card promises "click a marker to view region
  // details" and only the scroll-to-card half of that ever worked.
  const handleSelectRegion = React.useCallback((regionId: string) => {
    setSelectedRegion(regionId);
    // Expand the selected region
    setExpandedRegions((prev) => {
      const next = new Set(prev);
      next.add(regionId);
      return next;
    });
    // Move to the page the region is on, or the scroll below finds nothing to scroll to.
    const { regions, pageSize: size } = pagingRef.current;
    const index = regions.findIndex((r) => r.region_id === regionId);
    if (index >= 0) {
      setPage(Math.floor(index / size) + 1);
    }
    // Scroll to the region card
    setTimeout(() => {
      const element = document.getElementById(`region-card-${regionId}`);
      if (element) {
        element.scrollIntoView({ behavior: "smooth", block: "center" });
        // Add a brief highlight effect
        element.classList.add("ring-2", "ring-primary", "ring-offset-2");
        setTimeout(() => {
          element.classList.remove("ring-2", "ring-primary", "ring-offset-2");
        }, 2000);
      }
    }, 100);
  }, []);

  // View region on map - scroll to map and zoom to location
  const handleViewOnMap = (regionId: string) => {
    setSelectedRegion(regionId);
    setFocusSignal((n) => n + 1);
    // Scroll to map section
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  if (error) {
    return (
      <div className="container py-8">
        <Card className="border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive">Failed to load regions: {error instanceof Error ? error.message : String(error)}</p>
            <Button onClick={() => refetch()} className="mt-4">
              <RefreshCw className="h-4 w-4 mr-2" />
              Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container py-8 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold">Regions</h1>
          <p className="text-muted-foreground mt-1">
            Geographic overview of your cloud federation
          </p>
        </div>
        <RefreshButton
          label="Refresh regions"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      {/* Global stats */}
      {data && fleet && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <StatTile icon={Globe} label="Regions" value={data.regions.length} />
          <StatTile icon={Server} label="Clusters" value={data.total_nodes} />
          <StatTile icon={Cpu} label="CPU Threads" value={nothingRead ? NOT_READ : fleet.cpu} />
          <StatTile icon={MemoryStick} label="Memory" value={nothingRead ? NOT_READ : formatBytes(fleet.memory)} />
          <StatTile icon={HardDrive} label="Storage" value={nothingRead ? NOT_READ : formatBytes(fleet.storage)} />
          <StatTile
            icon={Sparkles}
            label="Compute GPUs"
            value={nothingRead ? NOT_READ : fleet.gpu}
            note={!nothingRead && fleet.gpuCards > fleet.gpu ? `${plural(fleet.gpuCards, "card")} detected` : undefined}
          />
        </div>
      )}
      {data && !fleet && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatTile icon={Globe} label="Regions" value={data.regions.length} />
          <StatTile icon={Server} label="Clusters" value={data.total_nodes} />
          <StatTile icon={Box} label="Instances" value={data.total_instances} />
          <StatTile icon={Activity} label="Running" value={runningInstances} />
        </div>
      )}

      {/* Map */}
      <RegionsMapCard
        regions={filteredRegions}
        selectedRegion={selectedRegion}
        focusSignal={focusSignal}
        onSelectRegion={handleSelectRegion}
        basemap={basemap}
        isLoading={isLoading}
      />

      {/* Region List */}
      <div className="space-y-4">
        {/* Revived. The controls were commented out with "will enable when more regions
            exist"; the two-value rule generalises that instinct, so a facet with nothing to
            choose between hides itself rather than needing a TODO. */}
        <TableToolbar filters={filters} noun="regions" />

        <div className="space-y-4">
          <h2 className="text-lg font-semibold">Regions</h2>

          {isLoading ? (
            <div className="space-y-4">
              {[1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-32 w-full" />
              ))}
            </div>
          ) : filteredRegions.length === 0 ? (
            <Card>
              <CardContent className="pt-6 text-center">
                {/* A scene only for the first-run state; a filter that matched nothing keeps the icon. */}
                {data?.regions.length === 0 ? (
                  <>
                    <RegionsIllustration size="md" className="mx-auto mb-4" />
                    <IllustrationFallback>
                      <Globe className="h-12 w-12 mx-auto text-muted-foreground mb-4" aria-hidden="true" />
                    </IllustrationFallback>
                  </>
                ) : (
                  <Globe className="h-12 w-12 mx-auto text-muted-foreground mb-4" aria-hidden="true" />
                )}
                <p className="text-muted-foreground">
                  {data?.regions.length === 0
                    ? "No clusters with location data are registered yet."
                    : "No regions match your filters."}
                </p>
                {filters.activeFilterCount > 0 && (
                  <>
                    <p className="text-xs text-muted-foreground mt-1">
                      {plural(filters.activeFilterCount, "filter")} active
                    </p>
                    <Button
                      variant="link"
                      onClick={filters.clearAll}
                      className="mt-2"
                    >
                      Clear filters
                    </Button>
                  </>
                )}
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-4">
              {pagedRegions.map((region) => (
                <RegionCard
                  key={region.region_id}
                  region={region}
                  showsHardware={fleet !== null}
                  isExpanded={expandedRegions.has(region.region_id)}
                  onToggle={() => toggleRegion(region.region_id)}
                  onViewOnMap={() => handleViewOnMap(region.region_id)}
                />
              ))}
            </div>
          )}

          {/* Absent rather than disabled when every region fits on one page. */}
          {totalPages > 1 && (
            <DataTablePagination
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              pageSizeOptions={[...GRID_PAGE_SIZES]}
              totalItems={filteredRegions.length}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(1);
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// The boundary sits next to the component that reads the query string rather than in the
// route file, so it stays visible to anyone editing this component and to the guard.
export function RegionsPage({ basemap }: { basemap: BasemapConfig }) {
  return (
    <React.Suspense fallback={<LoadingLottiePage />}>
      <RegionsContent basemap={basemap} />
    </React.Suspense>
  );
}
