import { derivePickerState } from "@/lib/picker-state";
import { projectSetsLimits, readProjectLimits } from "@/lib/lxd-limits";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import {
  useInstanceWritePolicy,
  useProjects,
  useProjectUsage,
  useStoragePoolList,
} from "@/hooks/use-lxd";
import type {
  ProjectInfo,
  ProjectListResponse,
  StoragePoolInfo,
  StoragePoolListResponse,
} from "@/types/api";

interface BaseInventoryInput {
  actorSelectionActive: boolean;
  host: string;
  project: string;
  clusterEpoch: number;
  selectionEpoch: number;
  seedValidationPending: boolean;
}

export function useBaseCreateInventory({
  actorSelectionActive,
  host,
  project,
  clusterEpoch,
  selectionEpoch,
  seedValidationPending,
}: BaseInventoryInput) {
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;
  const nodeLabel = host ? nodePicker.labelOf(host) : undefined;
  const writePolicy = useInstanceWritePolicy(host, {
    enabled: actorSelectionActive,
    requestEpoch: clusterEpoch,
  });
  const canUseAdvancedCreate =
    !writePolicy.isFetching &&
    !writePolicy.isError &&
    writePolicy.data?.host === host &&
    writePolicy.data.mode === "full" &&
    writePolicy.data.can_use_advanced_create === true;
  const hadFullAdvancedPolicy =
    writePolicy.data?.host === host &&
    writePolicy.data.mode === "full" &&
    writePolicy.data.can_use_advanced_create === true;
  const policyMutationDisabledReason = hadFullAdvancedPolicy && writePolicy.isFetching
    ? "Create is paused while the server rechecks the advanced write policy."
    : hadFullAdvancedPolicy && writePolicy.isError
      ? "Create is paused because the previously granted advanced write policy could not be verified. Reload or select the cluster again."
      : undefined;
  const advancedPolicyMessage = !host
    ? "Select a cluster to check which advanced controls are available."
    : writePolicy.isFetching
      ? "Checking the server policy. Safe typed controls remain available."
      : writePolicy.isError
        ? "Advanced controls are unavailable because the server policy could not be verified."
        : canUseAdvancedCreate
          ? null
          : "Advanced host controls are unavailable for this cluster. Safe resource, network, volume, metadata, and cloud-init fields remain available.";
  const nodeState = derivePickerState({
    isLoading: loadingNodes,
    isError: nodePicker.isError,
    error: nodePicker.namesError,
    optionCount: nodeOptions.length,
    subject: "Clusters",
    selectText: "Select a cluster",
    emptyText: "No clusters found.",
  });

  const projectsQuery = useProjects(host, {
    enabled: actorSelectionActive,
    requestEpoch: clusterEpoch,
  });
  const projectsResp = projectsQuery.data;
  const projectOptions = projectsQuery.isError
    ? []
    : Array.from(
        new Set([
          "default",
          ...(
            ((projectsResp as unknown as ProjectListResponse | undefined)?.projects ??
              []) as ProjectInfo[]
          ).map((item) => item.name),
        ]),
      ).map((name) => ({ value: name, label: name }));
  const projectState = derivePickerState({
    gated: !host,
    isLoading: projectsQuery.isLoading || (seedValidationPending && projectsQuery.isFetching),
    isError: projectsQuery.isError,
    error: projectsQuery.error,
    optionCount: projectOptions.length,
    subject: "Projects",
    nodeLabel,
    selectText: "Select project",
  });
  const selectedProjectInfo = (
    ((projectsResp as unknown as ProjectListResponse | undefined)?.projects ?? []) as ProjectInfo[]
  ).find((item) => item.name === project);
  const projectLimits = readProjectLimits(selectedProjectInfo?.config);
  const projectLimited = projectSetsLimits(projectLimits);
  const projectUsageQuery = useProjectUsage(host, project, {
    enabled: actorSelectionActive && !!host && projectLimited,
    requestEpoch: selectionEpoch,
  });
  const projectUsed = projectUsageQuery.data?.complete
    ? {
        cpu: projectUsageQuery.data.used.cpu,
        memoryBytes: projectUsageQuery.data.used.memory_bytes,
        instances: projectUsageQuery.data.used.instances,
      }
    : null;

  const poolsQuery = useStoragePoolList(host, {
    enabled: actorSelectionActive && !!host,
    requestEpoch: clusterEpoch,
  });
  const poolOptions = (
    ((poolsQuery.data as unknown as StoragePoolListResponse | undefined)?.pools ??
      []) as StoragePoolInfo[]
  ).map((pool) => ({ value: pool.name, label: pool.driver ? `${pool.name} (${pool.driver})` : pool.name }));
  const poolState = derivePickerState({
    gated: !host,
    isLoading: poolsQuery.isLoading,
    isError: poolsQuery.isError,
    error: poolsQuery.error,
    optionCount: poolOptions.length,
    subject: "Storage pools",
    nodeLabel,
    selectText: "Select storage pool",
  });
  const defaultPool =
    poolOptions.find((pool) => pool.value === "default")?.value || poolOptions[0]?.value || "";

  return {
    nodePicker,
    nodeOptions,
    loadingNodes,
    nodeLabel,
    nodeState,
    writePolicy,
    canUseAdvancedCreate,
    policyMutationDisabledReason,
    advancedPolicyMessage,
    projectsQuery,
    projectsResp,
    projectOptions,
    projectState,
    projectLimits,
    projectLimited,
    projectUsed,
    poolsQuery,
    poolOptions,
    poolState,
    defaultPool,
  };
}
