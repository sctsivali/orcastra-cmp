"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { RegionSummary } from "@/types/api";
import { BasemapConfig } from "@/lib/map-basemap";
import type { RegionsMapProps } from "./regions-map";

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

/**
 * A region the backend could not place.
 *
 * A cluster registered without location metadata comes back at latitude and longitude 0,
 * which is a real coordinate in the Gulf of Guinea rather than a null. Plotting it puts a
 * marker in the Atlantic, thousands of kilometres from anything, and the map's bounds keep
 * the operator from ever panning there to work out what it was.
 */
function isUnplaceable(region: RegionSummary): boolean {
  const { latitude, longitude } = region;
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return true;
  return latitude === 0 && longitude === 0;
}

interface RegionsMapCardProps {
  regions: RegionSummary[];
  selectedRegion: string | null;
  onSelectRegion: (regionId: string) => void;
  basemap: BasemapConfig;
  isLoading: boolean;
}

export function RegionsMapCard({
  regions,
  selectedRegion,
  onSelectRegion,
  basemap,
  isLoading,
}: RegionsMapCardProps) {
  const [tilesFellBack, setTilesFellBack] = React.useState(false);

  const placeable = React.useMemo(() => regions.filter((r) => !isUnplaceable(r)), [regions]);
  const unplaceableCount = regions.length - placeable.length;

  const handleBasemapFallback = React.useCallback(() => setTilesFellBack(true), []);

  return (
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
            regions={placeable}
            selectedRegion={selectedRegion}
            onSelectRegion={onSelectRegion}
            basemap={basemap}
            onBasemapFallback={handleBasemapFallback}
          />
        )}

        {/* Saying what the map is not showing, rather than dropping it in silence. */}
        {!isLoading && unplaceableCount > 0 && (
          <p className="text-xs text-muted-foreground mt-3">
            {unplaceableCount === 1
              ? "1 region has no location data and is not shown on the map."
              : `${unplaceableCount} regions have no location data and are not shown on the map.`}{" "}
            Set a location when registering the node, or from the cluster detail page.
          </p>
        )}

        {!isLoading && tilesFellBack && (
          <p className="text-xs text-muted-foreground mt-2">
            The configured map tiles could not be loaded, so the built-in outline map is
            shown instead.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
