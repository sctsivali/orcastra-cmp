"use client";

import { useCallback, useRef, useState } from "react";

type SourceType = "image" | "none";

/**
 * Own the create form's optional inventory reads and selection generation.
 *
 * The basic cluster/project/pool policy reads are handled by their selected-cluster keys. These
 * flags cover only expensive pickers that should stay idle until the operator expresses intent.
 */
export function useCreateInventoryIntent(seedSource?: SourceType) {
  const [localImages, setLocalImages] = useState(seedSource === "image");
  const [remoteImages, setRemoteImages] = useState(false);
  const [volumes, setVolumes] = useState(seedSource === "none");
  const [networks, setNetworks] = useState(false);
  const clusterEpochRef = useRef(0);
  const selectionEpochRef = useRef(0);

  const nextSelectionEpoch = useCallback(() => {
    selectionEpochRef.current += 1;
    return selectionEpochRef.current;
  }, []);

  const openImageSources = useCallback(() => {
    setLocalImages(true);
    setRemoteImages(true);
  }, []);

  const requireVolumes = useCallback(() => setVolumes(true), []);
  const requireNetworks = useCallback(() => setNetworks(true), []);
  const resetNetworks = useCallback(() => setNetworks(false), []);

  const resetForCluster = useCallback(() => {
    clusterEpochRef.current += 1;
    nextSelectionEpoch();
    setLocalImages(false);
    setRemoteImages(false);
    setVolumes(false);
    setNetworks(false);
  }, [nextSelectionEpoch]);

  return {
    localImages,
    remoteImages,
    volumes,
    networks,
    clusterEpochRef,
    selectionEpochRef,
    nextSelectionEpoch,
    openImageSources,
    requireVolumes,
    requireNetworks,
    resetNetworks,
    resetForCluster,
  };
}
