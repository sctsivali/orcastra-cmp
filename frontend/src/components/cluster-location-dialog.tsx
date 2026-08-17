"use client";

import * as React from "react";
import { Search, Check, Loader2, Globe } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { toast } from "@/hooks/use-toast";
import { useUpdateClusterLocation } from "@/hooks/use-lxd";
import { REGIONS_DATA, type Region } from "@/lib/regions-data";
import { cn } from "@/lib/utils";

// Filter Indonesia regions only
const INDONESIA_REGIONS = REGIONS_DATA.filter(r => r.country_code === "ID");

interface ClusterLocationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cluster: {
    cluster_id: string;
    display_name: string;
    location?: {
      region_id: string;
      region_name: string;
      city: string;
    };
  } | null;
  onSuccess?: () => void;
}

export function ClusterLocationDialog({
  open,
  onOpenChange,
  cluster,
  onSuccess,
}: ClusterLocationDialogProps) {
  const [searchQuery, setSearchQuery] = React.useState("");
  const [selectedRegion, setSelectedRegion] = React.useState<Region | null>(null);
  const updateLocation = useUpdateClusterLocation();

  // Filter regions based on search query
  const filteredRegions = React.useMemo(() => {
    if (!searchQuery.trim()) return INDONESIA_REGIONS;
    const query = searchQuery.toLowerCase();
    return INDONESIA_REGIONS.filter(
      (region: Region) =>
        region.name.toLowerCase().includes(query) ||
        region.city.toLowerCase().includes(query) ||
        region.country_name.toLowerCase().includes(query)
    );
  }, [searchQuery]);

  // Reset state when dialog opens/closes
  React.useEffect(() => {
    if (open) {
      setSearchQuery("");
      // Pre-select current location if exists
      if (cluster?.location?.region_id) {
        const currentRegion = INDONESIA_REGIONS.find(
          (r: Region) => r.id === cluster.location?.region_id
        );
        setSelectedRegion(currentRegion || null);
      } else {
        setSelectedRegion(null);
      }
    }
  }, [open, cluster]);

  // important: isPending is React state, so the disabled prop only updates on the next render.
  // Two clicks inside one tick both fire the mutation. Claimed synchronously, before any await.
  const submitInFlightRef = React.useRef(false);

  const handleSave = async () => {
    if (!cluster || !selectedRegion) return;
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;

    try {
      await updateLocation.mutateAsync({
        clusterId: cluster.cluster_id,
        location: {
          region_id: selectedRegion.id,
          region_name: selectedRegion.name,
          country_code: selectedRegion.country_code,
          country_name: selectedRegion.country_name,
          city: selectedRegion.city,
          latitude: selectedRegion.latitude,
          longitude: selectedRegion.longitude,
          timezone: selectedRegion.timezone,
          continent: selectedRegion.continent,
        },
      });

      toast({
        title: "Location Updated",
        description: `${cluster.display_name} is now located in ${selectedRegion.city}, ${selectedRegion.name}`,
      });

      onOpenChange(false);
      onSuccess?.();
    } catch (error: any) {
      toast({
        title: "Failed to Update Location",
        description: error.message || "An error occurred",
        variant: "destructive",
      });
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>
            Set Cluster Location
          </DialogTitle>
          <DialogDescription>
            {cluster && (
              <>
                Select the geographic location for{" "}
                <span className="font-semibold">{cluster.display_name}</span>
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4 pr-2">
          {/* Search Input */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search region, city, or province..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10"
            />
          </div>

          {/* Current Location */}
          {cluster?.location && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground bg-muted/50 p-2 rounded-md">
              <Globe className="h-4 w-4" />
              <span>
                Current: {cluster.location.city}, {cluster.location.region_name}
              </span>
            </div>
          )}

          {/* Region List */}
          <div className="min-h-[180px] max-h-[300px] overflow-y-auto border rounded-md [scrollbar-gutter:stable]">
            <div className="p-2 space-y-1">
              {filteredRegions.length === 0 ? (
                <div className="text-center py-8 text-muted-foreground">
                  No regions found matching "{searchQuery}"
                </div>
              ) : (
                filteredRegions.map((region: Region) => (
                  <button
                    key={region.id}
                    onClick={() => setSelectedRegion(region)}
                    className={cn(
                      "w-full flex items-center justify-between p-3 rounded-md text-left transition-colors",
                      selectedRegion?.id === region.id
                        ? "bg-primary text-primary-foreground"
                        : "hover:bg-muted"
                    )}
                  >
                    <div>
                      <div className="font-medium">{region.city}</div>
                      <div className={cn(
                        "text-xs",
                        selectedRegion?.id === region.id
                          ? "text-primary-foreground/80"
                          : "text-muted-foreground"
                      )}>
                        {region.name} • {region.country_name}
                      </div>
                    </div>
                    {selectedRegion?.id === region.id && (
                      <Check className="h-4 w-4" />
                    )}
                  </button>
                ))
              )}
            </div>
          </div>
        </DialogBody>

        <DialogFooter className="shrink-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            disabled={!selectedRegion || updateLocation.isPending}
          >
            {updateLocation.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Saving...
              </>
            ) : (
              "Set Location"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
