import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useReadOnlyMode() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["system-settings"],
    queryFn: api.system.getSettings,
    staleTime: 30000, // cache for 30 seconds
    refetchInterval: 60000, // refetch every minute
    retry: 3, // retry 3 times on failure before marking as error
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 10000), // exponential backoff
  });

  // On transient API errors (network hiccup, server restart), do NOT default to
  // read-only. Only enable read-only when the server explicitly returns it.
  // Previous behavior (isError → read-only) caused false positives during
  // brief connectivity drops, blocking admin/partner workflows unnecessarily.
  return {
    isReadOnly: data?.read_only_mode ?? false,
    isLoading,
    isError,
    apiVersion: data?.api_version,
  };
}
