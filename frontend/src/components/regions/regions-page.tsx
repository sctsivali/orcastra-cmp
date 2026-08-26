"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { clusterStatusMeta } from "@/lib/cluster-status";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatLabel } from "@/lib/utils";
import { GPU_EMPTY_TITLE, GPU_NO_COMPUTE_TITLE } from "@/lib/gpu";
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
  Loader2,
  Activity,
  Box,
  Building2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { useRegionsOverview } from "@/hooks/use-lxd";
import { RegionSummary, ExtendedHardwareInfo, ClusterStatus } from "@/types/api";

// Props interface for the map component
interface RegionsMapProps {
  regions: RegionSummary[];
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
}

// Dynamically import the map component to avoid SSR issues
const RegionsMap = dynamic<RegionsMapProps>(
  () => import("./regions-map").then((mod) => mod.default),
  {
    ssr: false,
    loading: () => (
      <div className="h-[400px] bg-muted/30 rounded-lg flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    ) }
);

// Format bytes to human readable
function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}


// Hardware summary component
function HardwareSummary({ hardware, compact = false }: { hardware: ExtendedHardwareInfo; compact?: boolean }) {
  // Compute GPUs and detected cards are different numbers about the same hardware, so the
  // card count only appears when it says something the GPU count did not.
  const cardsTotal = hardware.gpu_cards_total ?? hardware.gpu_total;
  const extraCards = Math.max(0, cardsTotal - hardware.gpu_total);
  if (compact) {
    return (
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge variant="outline" className="gap-1">
          <Cpu className="h-3 w-3 text-foreground" />
          {hardware.cpu_total} cores
        </Badge>
        <Badge variant="outline" className="gap-1">
          <MemoryStick className="h-3 w-3 text-foreground" />
          {formatBytes(hardware.memory_total)}
        </Badge>
        {hardware.gpu_total > 0 && (
          <Badge variant="outline" className="gap-1">
            <Sparkles className="h-3 w-3 text-foreground" />
            {hardware.gpu_total} GPU
          </Badge>
        )}
        <Badge variant="outline" className="gap-1">
          <HardDrive className="h-3 w-3 text-foreground" />
          {formatBytes(hardware.storage_total)}
        </Badge>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-4">
      {/* CPU */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Cpu className="h-4 w-4 text-foreground" />
          CPU
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          <p>{hardware.cpu_total} cores ({hardware.cpu_sockets} sockets)</p>
          <p>{hardware.cpu_architecture}</p>
          {hardware.cpu_vendor && <p>{hardware.cpu_vendor}</p>}
        </div>
      </div>

      {/* Memory */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <MemoryStick className="h-4 w-4 text-foreground" />
          Memory
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          <p>{formatBytes(hardware.memory_total)}</p>
          <p>{formatBytes(hardware.memory_used)} used</p>
        </div>
      </div>

      {/* GPU */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4 text-foreground" />
          GPU
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          {hardware.gpu_total > 0 ? (
            <>
              <p>
                {hardware.gpu_total} compute {hardware.gpu_total === 1 ? "GPU" : "GPUs"}
              </p>
              {hardware.gpu_vendors.length > 0 && (
                <p>{hardware.gpu_vendors.join(", ")}</p>
              )}
              {hardware.gpu_models.length > 0 && (
                <p className="break-words">{hardware.gpu_models.join(", ")}</p>
              )}
              {extraCards > 0 && <p className="text-xs">{cardsTotal} cards detected</p>}
            </>
          ) : cardsTotal > 0 ? (
            <>
              <p>{GPU_NO_COMPUTE_TITLE}</p>
              <p className="text-xs">{cardsTotal} cards detected</p>
            </>
          ) : (
            <p>{GPU_EMPTY_TITLE}</p>
          )}
        </div>
      </div>

      {/* Storage */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <HardDrive className="h-4 w-4 text-foreground" />
          Storage
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          <p>{formatBytes(hardware.storage_total)}</p>
          {hardware.storage_types.length > 0 && (
            <p>{hardware.storage_types.join(", ")}</p>
          )}
          {hardware.storage_drivers.length > 0 && (
            <p className="text-xs">{hardware.storage_drivers.join(", ")}</p>
          )}
        </div>
      </div>
    </div>
  );
}

// Region card component
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
  const onlineNodes = region.nodes.filter((n) => n.status === "online").length;
  
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
                    <Badge variant="outline" className="text-xs">
                      {region.country_code}
                    </Badge>
                  </CardTitle>
                  <CardDescription className="text-xs">
                    {region.city}, {region.country_name}
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
                  <p className="text-sm font-medium">{region.node_count} nodes</p>
                  <p className="text-xs text-muted-foreground">
                    {onlineNodes} online
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
            
            {/* Nodes list */}
            <div className="space-y-2">
              <p className="text-sm font-medium">Nodes in this region:</p>
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
                      <span>{node.hardware.cpu_total} CPU</span>
                      <span>•</span>
                      <span>{formatBytes(node.hardware.memory_total)}</span>
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

// Main Regions Page Component
// A card list rather than a table, so it takes the shared toolbar and filter engine but
// none of the table plumbing: no sortable heads, no pagination, no empty row.
const REGIONS = defineTable<any>()({
  searchPlaceholder: "Search regions",
  defaultSort: { field: "region_name", direction: "asc" },
  searchExtra: (r) => [r.city, r.country_name, r.country_code, r.facility_name, r.facility_provider],
  columns: [
    { field: "region_name", label: "Region", sort: (r) => r.region_name, search: (r) => r.region_name, filter: fromSort("text") },
    {
      field: "continent",
      label: "Continent",
      sort: (r) => r.continent,
      facet: (r) => r.continent,
      facetSpec: { primary: true, format: formatLabel } },
    {
      field: "country",
      label: "Country",
      sort: (r) => r.country_name,
      facet: (r) => r.country_name,
      facetSpec: {} },
    {
      field: "provider",
      label: "Facility Provider",
      sort: (r) => r.facility_provider,
      facet: (r) => r.facility_provider,
      facetSpec: { emptyLabel: "(no provider)" } },
    {
      field: "node_status",
      label: "Node Status",
      // A region matches when any of its nodes matches.
      facet: (r) => (r.nodes ?? []).map((n: any) => n.status).filter(Boolean),
      facetSpec: { primary: true, format: formatLabel, emptyLabel: "(no nodes)" } },
    {
      field: "node_type",
      label: "Node Type",
      facet: (r) => (r.nodes ?? []).map((n: any) => n.node_type).filter(Boolean),
      facetSpec: { emptyLabel: "(no nodes)" } },
  ] });

function RegionsContent() {
  const { data, isLoading, error, refetch, refreshNow, dataUpdatedAt } = useRegionsOverview();
  const [expandedRegions, setExpandedRegions] = React.useState<Set<string>>(new Set());
  const [selectedRegion, setSelectedRegion] = React.useState<string | null>(null);

  const filters = useTableFilters(REGIONS, (data?.regions ?? []) as any[]);
  const filteredRegions = filters.filteredRows;

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
  const handleSelectRegion = (regionId: string) => {
    setSelectedRegion(regionId);
    // Expand the selected region
    setExpandedRegions((prev) => {
      const next = new Set(prev);
      next.add(regionId);
      return next;
    });
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
  };

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
                <span className="text-sm text-muted-foreground">Nodes</span>
              </div>
              <p className="text-2xl font-bold mt-1">{data.total_nodes}</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4">
              <div className="flex items-center gap-2">
                <Cpu className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm text-muted-foreground">Total CPU</span>
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
              <p className="text-2xl font-bold mt-1">{formatBytes(data.total_memory)}</p>
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
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-lg">Cluster Locations</CardTitle>
          <CardDescription>
            Click on a marker to view region details below. Scroll to zoom.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-[400px] w-full" />
          ) : (
            <RegionsMap
              regions={filteredRegions}
              selectedRegion={selectedRegion}
              onSelectRegion={handleSelectRegion}
            />
          )}
        </CardContent>
      </Card>

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
                    ? "No nodes with location data registered yet."
                    : "No regions match your filters."}
                </p>
                {filters.activeFilterCount > 0 && (
                  <Button
                    variant="link"
                    onClick={filters.clearAll}
                    className="mt-2"
                  >
                    Clear filters
                  </Button>
                )}
              </CardContent>
            </Card>
          ) : (
            <ScrollArea className="h-[600px] pr-4">
              <div className="space-y-4">
                {filteredRegions.map((region) => (
                  <RegionCard
                    key={region.region_id}
                    region={region}
                    isExpanded={expandedRegions.has(region.region_id)}
                    onToggle={() => toggleRegion(region.region_id)}
                    onViewOnMap={() => handleViewOnMap(region.region_id)}
                  />
                ))}
              </div>
            </ScrollArea>
          )}
        </div>
      </div>
    </div>
  );
}

// The boundary sits next to the component that reads the query string rather than in the
// route file, so it stays visible to anyone editing this component and to the guard.
export function RegionsPage() {
  return (
    <React.Suspense fallback={<LoadingLottiePage />}>
      <RegionsContent />
    </React.Suspense>
  );
}
