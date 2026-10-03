"use client";

import { useQuery } from "@tanstack/react-query";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { fetchAPI } from "@/lib/api";
import type { NetworkACLListResponse } from "@/types/api";
import type { NetworkType } from "./network-rules";

export interface NetworkCapabilities {
  known: boolean;
  types: NetworkType[];
  config_keys: Record<NetworkType, string[] | undefined>;
}

export function useNetworkCreateOptions(node: string, project: string, type: NetworkType) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const capabilities = useQuery({
    queryKey: ["networkCreateCapabilities", scopeKey, node],
    queryFn: ({ signal }) => fetchAPI<NetworkCapabilities>(`/networks/creation/capabilities?host=${encodeURIComponent(node)}`, { signal }),
    enabled: isAuthenticated && !!node,
    staleTime: 0,
    retry: false,
  });
  // A failed recheck cannot authorize fields using an old capability response.
  const supportedKeys = capabilities.isError ? [] : capabilities.data?.config_keys[type] ?? [];
  const aclSupported = supportedKeys.includes("security.acls");
  const acls = useQuery({
    queryKey: ["networkCreateACLs", scopeKey, node, project],
    queryFn: ({ signal }) => fetchAPI<NetworkACLListResponse>(`/network-acls?host=${encodeURIComponent(node)}&project=${encodeURIComponent(project)}`, { signal }),
    enabled: isAuthenticated && !!node && aclSupported,
    staleTime: 0,
    retry: false,
  });
  const aclIncomplete = !!acls.data?.unreachable_nodes?.length || !!acls.data?.partial_nodes?.length;
  const aclNames = Array.from(new Set((acls.data?.acls ?? []).filter((acl) => !acl.node || acl.node === node).map((acl) => acl.name))).sort();
  return { capabilities, supportedKeys, aclSupported, acls, aclIncomplete, aclNames };
}
