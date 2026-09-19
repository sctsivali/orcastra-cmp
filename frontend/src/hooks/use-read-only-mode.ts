import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * The one read of `/system/settings`, shared by every caller through its query key.
 *
 * The endpoint answers both a mode (read-only) and a capability (whether the schedule routes are
 * mounted), and both are already polled on the same minute. A second hook with its own `useQuery`
 * would share the cache but not the options, so the two would drift apart on the next edit.
 */
function useSystemSettings() {
  return useQuery({
    queryKey: ["system-settings"],
    queryFn: api.system.getSettings,
    staleTime: 30000, // cache for 30 seconds
    refetchInterval: 60000, // refetch every minute
    retry: 3, // retry 3 times on failure before marking as error
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 10000), // exponential backoff
  });
}

export function useReadOnlyMode() {
  const { data, isLoading, isError } = useSystemSettings();

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

/**
 * Whether this deployment mounted the snapshot schedule routes.
 *
 * important: three states, not two. `undefined` means not yet read, or read from a backend that
 * predates the field, and a control must stay as it was in that case rather than disable itself on
 * a page load. Only an explicit `false` is a deployment saying the routes are not there.
 */
export function useSnapshotSchedulesEnabled(): boolean | undefined {
  return useSystemSettings().data?.snapshot_schedules_enabled;
}

/**
 * Whether this deployment mounted the task journal routes, on the same three-state rule: only
 * an explicit `false` hides the task panel.
 */
export function useTaskJournalEnabled(): boolean | undefined {
  return useSystemSettings().data?.task_journal_enabled;
}
