import { useMemo } from "react";
import type { DiskDevice } from "@/components/instance-create/devices";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { derivePickerState } from "@/lib/picker-state";
import type { StorageVolumeInfo, StorageVolumeListResponse } from "@/types/api";

interface VolumeInventoryInput {
  actorSelectionActive: boolean;
  host: string;
  project: string;
  nodeLabel?: string;
  clusterEpoch: number;
  enabledByIntent: boolean;
  seedValidationPending: boolean;
}

export function useCreateVolumeInventory({
  actorSelectionActive,
  host,
  project,
  nodeLabel,
  clusterEpoch,
  enabledByIntent,
  seedValidationPending,
}: VolumeInventoryInput) {
  const query = useStorageVolumes(undefined, host, {
    enabled: actorSelectionActive && !!host && enabledByIntent,
    requestEpoch: clusterEpoch,
  });
  const allCustomVolumes = useMemo(
    () => (
      ((query.data as unknown as StorageVolumeListResponse | undefined)?.volumes ??
        []) as StorageVolumeInfo[]
    ).filter((volume) => volume.type === "custom"),
    [query.data],
  );
  // LXD can return the same per-member volume several times. The target project sees its own
  // custom volumes, or the default project's set when it has no project-local volume store.
  const customVolumes = useMemo(() => {
    const scoped = allCustomVolumes.filter((volume) => (volume.project ?? "default") === project);
    const visible = scoped.length
      ? scoped
      : allCustomVolumes.filter((volume) => (volume.project ?? "default") === "default");
    const seen = new Set<string>();
    return visible.filter((volume) => {
      const key = `${volume.pool}/${volume.name}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [allCustomVolumes, project]);

  const isoVisibleIn = (targetProject: string, pool: string, name: string) => {
    const isoRows = allCustomVolumes.filter((volume) => volume.content_type === "iso");
    const scoped = isoRows.filter(
      (volume) => (volume.project ?? "default") === targetProject,
    );
    const visible = scoped.length
      ? scoped
      : isoRows.filter((volume) => (volume.project ?? "default") === "default");
    return visible.some((volume) => volume.pool === pool && volume.name === name);
  };
  const volumesInPool = (pool: string) =>
    customVolumes.filter((volume) => volume.pool === pool);
  const volumeContentType = (pool: string, name: string) =>
    customVolumes.find((volume) => volume.pool === pool && volume.name === name)?.content_type;
  const volumesNotice = derivePickerState({
    gated: !host,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    optionCount: customVolumes.length,
    subject: "Custom volumes",
    nodeLabel,
  }).notice;
  const volumeStateFor = (disk: DiskDevice, poolVolumes: StorageVolumeInfo[]) =>
    derivePickerState({
      gated: !disk.pool,
      gatedText: "Select pool first",
      isLoading: query.isLoading,
      isError: query.isError,
      error: query.error,
      optionCount: poolVolumes.length,
      subject: "Custom volumes",
      emptyText: `No custom volumes in ${disk.pool}.`,
      selectText: "Select volume",
    });

  const isoVolumes = customVolumes.filter((volume) => volume.content_type === "iso");
  const isoOptions = isoVolumes.map((volume) => ({
    value: `${volume.pool}/${volume.name}`,
    label: `${volume.name} (${volume.pool})`,
  }));
  const isoState = derivePickerState({
    gated: !host,
    isLoading: query.isLoading || (seedValidationPending && query.isFetching),
    isError: query.isError,
    error: query.error,
    optionCount: isoOptions.length,
    subject: "ISO images",
    nodeLabel,
    emptyText: "No ISO images are available on this cluster.",
    selectText: "Select an ISO",
  });

  return {
    query,
    customVolumes,
    isoVisibleIn,
    volumesInPool,
    volumeContentType,
    volumesNotice,
    volumeStateFor,
    isoVolumes,
    isoOptions,
    isoState,
  };
}
