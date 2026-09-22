import { useEffect, useMemo, type Dispatch, type SetStateAction } from "react";
import type { NicDevice } from "@/components/instance-create/devices";
import { useNetworks, useProfile } from "@/hooks/use-lxd";
import { derivePickerState } from "@/lib/picker-state";
import { safeRandomUUID } from "@/lib/utils";
import type { NetworkInfo, NetworkListResponse, ProfileInfo } from "@/types/api";

interface NetworkInventoryInput {
  actorSelectionActive: boolean;
  host: string;
  project: string;
  nodeLabel?: string;
  selectionEpoch: number;
  enabledByIntent: boolean;
  pendingNicAdd: boolean;
  setPendingNicAdd: Dispatch<SetStateAction<boolean>>;
  setNics: Dispatch<SetStateAction<NicDevice[]>>;
}

export function useCreateNetworkInventory({
  actorSelectionActive,
  host,
  project,
  nodeLabel,
  selectionEpoch,
  enabledByIntent,
  pendingNicAdd,
  setPendingNicAdd,
  setNics,
}: NetworkInventoryInput) {
  const networksQuery = useNetworks(host, project || undefined, {
    enabled: actorSelectionActive && !!host && enabledByIntent,
    requestEpoch: selectionEpoch,
  });
  const projectNetworks = useMemo(
    () => (
      ((networksQuery.data as unknown as NetworkListResponse | undefined)?.networks ??
        []) as NetworkInfo[]
    ).filter((network) => network.managed),
    [networksQuery.data],
  );
  const networkOptions = projectNetworks.map((network) => ({
    value: network.name,
    label: `${network.name} (${network.type})`,
  }));
  const networkState = derivePickerState({
    gated: !host,
    isLoading: networksQuery.isLoading || (pendingNicAdd && networksQuery.isFetching),
    isError: networksQuery.isError,
    error: networksQuery.error,
    optionCount: networkOptions.length,
    subject: "Managed networks",
    nodeLabel,
    emptyText: `No managed networks in project ${project || "default"}.`,
    selectText: "Select network",
  });
  const networksByName = useMemo(
    () => new Map(projectNetworks.map((network) => [network.name, network])),
    [projectNetworks],
  );

  const defaultProfileQuery = useProfile("default", host, project || undefined, {
    enabled: actorSelectionActive && !!host && enabledByIntent,
    requestEpoch: selectionEpoch,
  });
  const inheritedNics = useMemo(
    () => Object.entries(
      ((defaultProfileQuery.data as unknown as ProfileInfo | undefined)?.devices ?? {}) as Record<
        string,
        Record<string, string>
      >,
    )
      .filter(([, device]) => device.type === "nic")
      .map(([name, device]) => ({ name, network: device.network || device.parent || "" })),
    [defaultProfileQuery.data],
  );
  const defaultProfileNotice = derivePickerState({
    gated: !host,
    isLoading: defaultProfileQuery.isFetching,
    isError: defaultProfileQuery.isError,
    error: defaultProfileQuery.error,
    optionCount: defaultProfileQuery.isSuccess ? 1 : 0,
    subject: "Default profile",
    nodeLabel,
  }).notice;

  // Attach is two-phase. Never fabricate eth0 from an error or an incomplete inherited profile.
  useEffect(() => {
    if (!pendingNicAdd) return;
    if (networksQuery.isFetching || defaultProfileQuery.isFetching) return;
    if (networksQuery.isError || defaultProfileQuery.isError) return;
    if (!networksQuery.isSuccess || !defaultProfileQuery.isSuccess) return;
    setPendingNicAdd(false);
    setNics((current) => {
      const used = new Set(current.map((nic) => nic.name.trim()));
      const inherited = inheritedNics.find((nic) => !used.has(nic.name));
      return [
        ...current,
        {
          id: safeRandomUUID(),
          network: inherited?.network ?? "",
          name: inherited?.name ?? `eth${current.length}`,
        },
      ];
    });
  }, [
    defaultProfileQuery.isError,
    defaultProfileQuery.isFetching,
    defaultProfileQuery.isSuccess,
    inheritedNics,
    networksQuery.isError,
    networksQuery.isFetching,
    networksQuery.isSuccess,
    pendingNicAdd,
    setNics,
    setPendingNicAdd,
  ]);

  return {
    networksQuery,
    projectNetworks,
    networkOptions,
    networkState,
    networksByName,
    defaultProfileQuery,
    inheritedNics,
    defaultProfileNotice,
  };
}
