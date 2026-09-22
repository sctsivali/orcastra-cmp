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
import { formatBytes, formatMemory } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger } from "@/components/ui/collapsible";
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
  Building2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useRegionsOverview } from "@/hooks/use-lxd";
import { HardwareSummary } from "./regions-hardware-summary";
import { BasemapConfig } from "@/lib/map-basemap";
import { RegionsMapCard } from "./regions-map-card";
import { REGIONS } from "./regions-table-config";
import { RegionSummary } from "@/types/api";

function RegionCard({
  region,
  isExpanded,
  onToggle,
  onViewOnMap }: {
  region: RegionSummary;
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
                  <MapPin className="h-5 w-5" />
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
                    {hasCountry ? `${region.city}, ${region.country_name}` : "No location data"}
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
                  <p className="text-sm font-medium">{region.node_count} clusters</p>
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
            
            {/* Quick stats */}
            <div className="mt-3 pt-3 border-t">
              <HardwareSummary hardware={region.hardware} compact />
            </div>
          </CardHeader>
        </CollapsibleTrigger>
        
        <CollapsibleContent>
          <CardContent className="pt-0 space-y-4">
            {/* Instance stats */}
            <div className="flex items-center gap-4 text-sm">
              <div className="flex items-center gap-1.5">
                <Box className="h-4 w-4 text-foreground" />
                <span>{region.instance_count} instances</span>
              </div>
              <div className="flex items-center gap-1.5">
                <Activity className="h-4 w-4 text-foreground" />
                <span>{region.instance_running} running</span>
              </div>
            </div>
            
            {/* Detailed hardware */}
            <HardwareSummary hardware={region.hardware} />
            
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
                      <span
                        aria-hidden
                        className={cn(
                          "h-2 w-2 rounded-full",
                          clusterStatusMeta(node.status).dotClassName
                        )}
                      />
                      {/* The dot used to be the only signal here, unlabelled and with no
                          dark: variants, so in dark mode a grey cluster was invisible and no
                          screen reader ever heard the state. */}
                      <span className="sr-only">
                        {clusterStatusMeta(node.status).srLabel}
                      </span>
                      <span className="font-medium">{node.display_name}</span>
                      {node.node_type && (
                        <Badge variant="secondary" className="text-xs">
                          {node.node_type}
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{node.hardware.cpu_total} hardware threads</span>
                      <span>•</span>
                      <span>{formatMemory(node.hardware.memory_total)}</span>
                      {node.hardware.gpu_total > 0 && (
                        <>
                          <span>•</span>
                          <span>{node.hardware.gpu_total} GPU</span>
                        </>
                      )}
                    </div>
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

  const [page, setPage] = React.useState(1);
  const [pageSize, setPageSize] = React.useState<number>(GRID_PAGE_SIZE);
  const resetPage = React.useCallback(() => setPage(1), []);

  const filters = useTableFilters(REGIONS, (data?.regions ?? []) as any[], {
    onFilterChange: resetPage,
  });
  const filteredRegions = filters.filteredRows;

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
    // Scroll to map section
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  if (error) {
    return (
      <div className="container py-8">
        <Card className="border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive">Failed to load regions: {String(error)}</p>
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
      {data && (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <Globe className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Regions</span>
              </div>
              <p className="text-2xl font-bold mt-1">{data.regions.length}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <Server className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Clusters</span>
              </div>
              <p className="text-2xl font-bold mt-1">{data.total_nodes}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <Cpu className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Hardware Threads</span>
              </div>
              <p className="text-2xl font-bold mt-1">{data.total_cpu}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <MemoryStick className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Memory</span>
              </div>
              <p className="text-2xl font-bold mt-1">{formatMemory(data.total_memory)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <HardDrive className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Storage</span>
              </div>
              <p className="text-2xl font-bold mt-1">{formatBytes(data.total_storage)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Compute GPUs</span>
              </div>
              <p className="text-2xl font-bold mt-1">{data.total_gpu}</p>
              {(data.total_gpu_cards ?? 0) > data.total_gpu && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  {data.total_gpu_cards} cards detected
                </p>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* Map */}
      <RegionsMapCard
        regions={filteredRegions}
        selectedRegion={selectedRegion}
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
                <Globe className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
                <p className="text-muted-foreground">
                  {data?.regions.length === 0
                    ? "No clusters with location data are registered yet."
                    : "No regions match your filters."}
                </p>
                {filters.activeFilterCount > 0 && (
                  <>
                    <p className="text-xs text-muted-foreground mt-1">
                      {filters.activeFilterCount} filter
                      {filters.activeFilterCount === 1 ? "" : "s"} active
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
