"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { jitter } from "@/lib/query-config";
import { releaseAPI } from "@/lib/release-api";

export const RELEASE_STATUS_KEY = "release-status";
const POLL_MS = 30 * 60 * 1000;

/**
 * What the release manifest says about the running version. Administrators only; no other role
 * sends the request. The backend reads the feed twice a day; this only reads its last answer.
 */
export function useReleaseStatus() {
  const { isAdmin, isLoading } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const [interval] = useState(() => jitter(POLL_MS));
  const query = useQuery({
    queryKey: [RELEASE_STATUS_KEY, scopeKey],
    queryFn: ({ signal }) => releaseAPI.get({ signal }),
    enabled: isAuthenticated && !isLoading && isAdmin,
    staleTime: 5 * 60 * 1000,
    retry: 1,
    refetchInterval: interval,
  });
  return { ...query, scopeKey, isAdmin };
}
