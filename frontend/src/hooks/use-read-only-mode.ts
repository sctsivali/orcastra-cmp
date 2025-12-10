import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useReadOnlyMode() {
  const { data, isLoading } = useQuery({
    queryKey: ["system-settings"],
    queryFn: api.system.getSettings,
    staleTime: 30000, // Cache for 30 seconds
    refetchInterval: 60000, // Refetch every minute
  });

  return {
    isReadOnly: data?.read_only_mode ?? false,
    isLoading,
    apiVersion: data?.api_version,
  };
}
