"use client";

import * as React from "react";
import { useMap } from "react-leaflet";
import L from "leaflet";
import { useTheme } from "@/components/theme-provider";
import { OFFLINE_BASEMAP_ATTRIBUTION, OFFLINE_BASEMAP_URL, loadGeoAsset } from "@/lib/map-basemap";
import { themeColor } from "./map-icons";

/**
 * The built-in landmass layer, drawn from a file this repository ships rather than from a
 * tile service.
 *
 * This is the default basemap. It costs one same-origin request, works in an air-gapped
 * install, cannot be watermarked or rate-limited by a third party, and leaks nothing about
 * which coordinates an operator is looking at, which on this page is where their
 * infrastructure physically sits.
 */

function loadBasemap(): Promise<GeoJSON.FeatureCollection> {
  return loadGeoAsset(OFFLINE_BASEMAP_URL, (data) => data as GeoJSON.FeatureCollection);
}

interface OfflineBasemapProps {
  onLoadError?: () => void;
}

export function OfflineBasemap({ onLoadError }: OfflineBasemapProps) {
  const map = useMap();
  const { resolvedTheme } = useTheme();
  const layerRef = React.useRef<L.GeoJSON | null>(null);

  const onLoadErrorRef = React.useRef(onLoadError);
  React.useEffect(() => {
    onLoadErrorRef.current = onLoadError;
  }, [onLoadError]);

  React.useEffect(() => {
    if (!map) return;

    let cancelled = false;

    loadBasemap()
      .then((data) => {
        if (cancelled) return;

        const layer = L.geoJSON(data, {
          attribution: OFFLINE_BASEMAP_ATTRIBUTION,
          // The backdrop must never swallow a marker click.
          interactive: false,
          style: {
            // note: canvas rather than the default SVG renderer. The vendored outline is
            // about 30k vertices, which SVG turns into 30k DOM nodes and a visibly slow
            // pan. It belongs in `style` rather than beside it because GeoJSON applies the
            // style before adding the layer, and Path.beforeAdd is what reads
            // options.renderer, so it lands in time either way and only this spelling is
            // in the published types.
            renderer: L.canvas({ padding: 0.5 }),
            fillColor: themeColor("--muted"),
            fillOpacity: 1,
            // note: the coastline is what makes this readable, not the fill. Land and water
            // are only five points of lightness apart in the light theme, so --border (which
            // is closer still) left the shapes barely visible. --muted-foreground is far
            // enough from both fills to draw an edge in either theme.
            color: themeColor("--muted-foreground", 0.45),
            weight: 0.8,
            opacity: 1,
          },
        });

        layer.addTo(map);
        layerRef.current = layer;
      })
      .catch(() => {
        if (cancelled) return;
        onLoadErrorRef.current?.();
      });

    return () => {
      cancelled = true;
      if (layerRef.current) {
        layerRef.current.remove();
        layerRef.current = null;
      }
    };
  }, [map]);

  // Canvas colours are baked in at draw time, so a theme switch has to restyle explicitly.
  // Without this the land keeps the palette it was first drawn with.
  React.useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return;
    layer.setStyle({
      fillColor: themeColor("--muted"),
      color: themeColor("--muted-foreground", 0.45),
    });
  }, [resolvedTheme]);

  return null;
}
