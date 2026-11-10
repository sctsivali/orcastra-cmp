import { RegionsPage } from "@/components/regions";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { resolveBasemapConfig } from "@/lib/map-basemap";

/**
 * The basemap is resolved in this server component, so MAP_TILE_* are runtime environment
 * variables: an operator changes one and restarts the container, with no rebuild.
 *
 * force-dynamic makes that hold. Without it Next prerenders the route at build time and the
 * value baked into the image wins. The page is behind auth and has nothing worth caching.
 */
export const dynamic = "force-dynamic";

export default function RegionsPageRoute() {
  const basemap = resolveBasemapConfig(process.env);

  return (
    <>
      <ReadOnlyBanner />
      <RegionsPage basemap={basemap} />
    </>
  );
}
