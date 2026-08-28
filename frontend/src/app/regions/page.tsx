import { RegionsPage } from "@/components/regions";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { resolveBasemapConfig } from "@/lib/map-basemap";

/**
 * The basemap is resolved here, in a server component, so MAP_TILE_* are ordinary runtime
 * environment variables: an operator changes one and restarts the container, with no
 * rebuild and no image of their own.
 *
 * force-dynamic is what makes that true. Without it Next prerenders this route at build
 * time and the value baked into the image wins forever. The page is behind auth and holds
 * no static content worth caching, so the render mode costs nothing.
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
