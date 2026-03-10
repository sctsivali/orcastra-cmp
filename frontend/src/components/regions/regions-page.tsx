"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
  Search,
  ChevronDown,
  ChevronUp,
  RefreshCw,
  MapPin,
  Loader2,
  Activity,
  Box,
  Filter,
  Building2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useRegionsOverview } from "@/hooks/use-lxd";
import { RegionSummary, ExtendedHardwareInfo, ClusterStatus } from "@/types/api";
import { CONTINENTS } from "@/lib/regions-data";

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
    ),
  }
);

// Format bytes to human readable
function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

// Get status color
function getStatusColor(status: ClusterStatus): string {
  switch (status) {
    case "online":
      return "bg-emerald-500";
    case "offline":
      return "bg-red-500";
    case "not_authorized":
      return "bg-amber-500";
    default:
      return "bg-gray-500";
  }
}

// Hardware summary component
function HardwareSummary({ hardware, compact = false }: { hardware: ExtendedHardwareInfo; compact?: boolean }) {
  if (compact) {
    return (
      <div className="flex flex-wrap gap-2 text-xs">
        <Badge variant="outline" className="gap-1">
          <Cpu className="h-3 w-3" />
          {hardware.cpu_total} cores
        </Badge>
        <Badge variant="outline" className="gap-1">
          <MemoryStick className="h-3 w-3" />
          {formatBytes(hardware.memory_total)}
        </Badge>
        {hardware.gpu_total > 0 && (
          <Badge variant="outline" className="gap-1">
            <Sparkles className="h-3 w-3" />
            {hardware.gpu_total} GPU
          </Badge>
        )}
        <Badge variant="outline" className="gap-1">
          <HardDrive className="h-3 w-3" />
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
          <Cpu className="h-4 w-4 text-blue-500" />
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
          <MemoryStick className="h-4 w-4 text-green-500" />
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
          <Sparkles className="h-4 w-4 text-purple-500" />
          GPU
        </div>
        <div className="pl-6 text-sm text-muted-foreground">
          {hardware.gpu_total > 0 ? (
            <>
              <p>{hardware.gpu_total} cards</p>
              {hardware.gpu_vendors.length > 0 && (
                <p>{hardware.gpu_vendors.join(", ")}</p>
              )}
            </>
          ) : (
            <p className="text-muted-foreground/50">No GPU</p>
          )}
        </div>
      </div>

      {/* Storage */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-sm font-medium">
          <HardDrive className="h-4 w-4 text-orange-500" />
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
  onViewOnMap,
}: {
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
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                  <MapPin className="h-5 w-5 text-primary" />
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
                        {region.facility_provider && ` — ${region.facility_provider}`}
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
                <Box className="h-4 w-4 text-muted-foreground" />
                <span>{region.instance_count} instances</span>
              </div>
              <div className="flex items-center gap-1.5">
                <Activity className="h-4 w-4 text-emerald-500" />
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
                      <span className={cn("h-2 w-2 rounded-full", getStatusColor(node.status))} />
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
export function RegionsPage() {
  const { data, isLoading, error, refetch, isRefetching } = useRegionsOverview();
  const [searchQuery, setSearchQuery] = React.useState("");
  const [continentFilter, setContinentFilter] = React.useState<string>("all");
  const [expandedRegions, setExpandedRegions] = React.useState<Set<string>>(new Set());
  const [selectedRegion, setSelectedRegion] = React.useState<string | null>(null);

  // Filter regions
  const filteredRegions = React.useMemo(() => {
    if (!data?.regions) return [];
    
    return data.regions.filter((region) => {
      // Search filter
      if (searchQuery) {
        const query = searchQuery.toLowerCase();
        const matchesSearch =
          region.region_name.toLowerCase().includes(query) ||
          region.city.toLowerCase().includes(query) ||
          region.country_name.toLowerCase().includes(query) ||
          region.country_code.toLowerCase().includes(query) ||
          (region.facility_name && region.facility_name.toLowerCase().includes(query)) ||
          (region.facility_provider && region.facility_provider.toLowerCase().includes(query));
        if (!matchesSearch) return false;
      }
      
      // Continent filter
      if (continentFilter !== "all" && region.continent !== continentFilter) {
        return false;
      }
      
      return true;
    });
  }, [data?.regions, searchQuery, continentFilter]);

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
          <h1 className="text-3xl font-bold flex items-center gap-3">
            Regions
          </h1>
          <p className="text-muted-foreground mt-1">
            Geographic overview of your cloud federation
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => refetch()}
          disabled={isRefetching}
        >
          <RefreshCw className={cn("h-4 w-4 mr-2", isRefetching && "animate-spin")} />
          Refresh
        </Button>
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
                <span className="text-sm text-muted-foreground">GPUs</span>
              </div>
              <p className="text-2xl font-bold mt-1">{data.total_gpu}</p>
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
        {/* Filters sidebar - disabled for now, will enable when more regions exist */}
        {/* TODO: Enable filters when expanding scope
        <Card className="h-fit">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <Filter className="h-4 w-4" />
              Filters
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">Search</label>
              <div className="relative">
                <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
                <Input
                  placeholder="Search regions..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-8"
                />
              </div>
            </div>
            <div className="pt-4 border-t">
              <p className="text-sm text-muted-foreground">
                Showing {filteredRegions.length} of {data?.regions.length || 0} regions
              </p>
            </div>
          </CardContent>
        </Card>
        */}

        {/* Regions list - full width since filters are hidden */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Regions</h2>
            <Badge variant="secondary">{filteredRegions.length} regions</Badge>
          </div>

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
                {searchQuery && (
                  <Button
                    variant="link"
                    onClick={() => setSearchQuery("")}
                    className="mt-2"
                  >
                    Clear search
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
