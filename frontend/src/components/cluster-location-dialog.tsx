"use client";

import * as React from "react";
import { Loader2, Globe } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import { toast } from "@/hooks/use-toast";
import { useUpdateClusterLocation } from "@/hooks/use-lxd";
import { RegionPicker, useRegionCatalog } from "@/components/regions/region-picker";
import { resolveRegion, toGeoLocation, type Region } from "@/lib/regions/catalog";
import { isWholeCountry, placeDetail, placeName } from "@/lib/regions/place";
import type { GeoLocation } from "@/types/api";

interface ClusterLocationDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  cluster: {
    cluster_id: string;
    display_name: string;
    location?: GeoLocation | null;
  } | null;
  onSuccess?: () => void;
}

export function ClusterLocationDialog({
  open,
  onOpenChange,
  cluster,
  onSuccess,
}: ClusterLocationDialogProps) {
  const [selected, setSelected] = React.useState<Region | null>(null);
  const { data: catalog } = useRegionCatalog();
  const updateLocation = useUpdateClusterLocation();
  const current = cluster?.location ?? null;

  // Pre-select the stored location; a retired id resolves to the entry that replaced it, so
  // saving without touching the picker also migrates the cluster to the current catalogue.
  React.useEffect(() => {
    if (!open) return;
    setSelected(catalog ? resolveRegion(catalog, current?.region_id) ?? null : null);
  }, [open, catalog, current?.region_id]);

  // important: isPending is React state, so the disabled prop only updates on the next render.
  // Two clicks inside one tick both fire the mutation. Claimed synchronously, before any await.
  const submitInFlightRef = React.useRef(false);

  const handleSave = async () => {
    if (!cluster || !selected) return;
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;

    try {
      // note: the PATCH replaces the whole location, so the facility recorded at registration
      // travels with it. Leaving it out used to erase it on every location edit.
      await updateLocation.mutateAsync({
        clusterId: cluster.cluster_id,
        location: toGeoLocation(selected, {
          facility_name: current?.facility_name,
          facility_provider: current?.facility_provider,
        }),
      });
      toast({
        title: "Location Updated",
        description: `${cluster.display_name} is now placed in ${placeName(selected)}.`,
      });
      onOpenChange(false);
      onSuccess?.();
    } catch (error: unknown) {
      toast({
        title: "Failed to Update Location",
        description: error instanceof Error ? error.message : "An error occurred",
        variant: "destructive",
      });
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0 pb-4">
          <DialogTitle>Set Cluster Location</DialogTitle>
          <DialogDescription>
            {cluster && (
              <>
                Where <span className="font-semibold">{cluster.display_name}</span> runs. A regency
                or city is pinned on the regions map; a country is shaded as a whole.
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4 pr-2">
          {current && (
            <div className="flex items-center gap-2 rounded-md bg-muted/50 p-2 text-sm text-muted-foreground">
              <Globe className="h-4 w-4 shrink-0" aria-hidden />
              <span>
                Current: {isWholeCountry(current) ? `${current.country_name} (whole country)` : placeName(current)}
              </span>
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="cluster-location-region">Region</Label>
            <RegionPicker
              id="cluster-location-region"
              value={selected?.id ?? ""}
              onValueChange={setSelected}
              disabled={updateLocation.isPending}
            />
            {selected && (
              <p className="text-xs text-muted-foreground">
                {isWholeCountry(selected)
                  ? `Shown on the map as the whole of ${selected.country_name}, because the exact site is not recorded.`
                  : `${placeDetail(selected)} (${selected.latitude}, ${selected.longitude})`}
              </p>
            )}
          </div>
        </DialogBody>

        <DialogFooter className="shrink-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={!selected || updateLocation.isPending}>
            {updateLocation.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Saving…
              </>
            ) : (
              "Set location"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
