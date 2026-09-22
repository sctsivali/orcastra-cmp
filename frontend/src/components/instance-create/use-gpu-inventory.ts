import { useNodeGpus } from "@/hooks/use-lxd";

interface GpuInventoryInput {
  actorSelectionActive: boolean;
  canUseAdvancedCreate: boolean;
  host: string;
  pickerOpen: boolean;
  clusterEpoch: number;
}

export function useCreateGpuInventory({
  actorSelectionActive,
  canUseAdvancedCreate,
  host,
  pickerOpen,
  clusterEpoch,
}: GpuInventoryInput) {
  const query = useNodeGpus(canUseAdvancedCreate ? host : "", {
    enabled: actorSelectionActive && canUseAdvancedCreate && !!host && pickerOpen,
    requestEpoch: clusterEpoch,
  });
  return {
    query,
    availableGpus: query.data?.gpus ?? [],
  };
}
