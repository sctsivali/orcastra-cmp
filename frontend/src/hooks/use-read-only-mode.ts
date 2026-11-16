import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { APIError } from "@/types/api";

/**
 * The one read of `/system/settings`, shared by every caller through its query key.
 *
 * The endpoint answers a mode (read-only) and capabilities (ex; whether the schedule routes are
 * mounted). A second hook with its own `useQuery` would share the cache but not the options.
 */
function useSystemSettings() {
  return useQuery({
    queryKey: ["system-settings"],
    queryFn: api.system.getSettings,
    staleTime: 30000,
    refetchInterval: 60000,
    retry: 3,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 10000),
  });
}

/**
 * How long a write verdict keeps answering while later polls fail. A minute's poll that meets a
 * token rotation or a dropped tunnel must not switch every write control off; a deployment that
 * stops answering for this long has to be read again before anything is changed.
 */
const WRITE_VERDICT_MAX_AGE_MS = 5 * 60 * 1000;

export type WriteAvailability = "checking" | "unavailable" | "read_only" | "available";

/** Why the verdict is missing, in words an operator can act on. */
function unavailableCause(error: unknown): string {
  const status = error instanceof APIError ? error.status : undefined;
  if (status === 401 || status === 403) return "Your session could not be confirmed. Sign in again";
  return "The server did not answer the settings check";
}

export function useReadOnlyMode() {
  const { data, dataUpdatedAt, isLoading, isFetching, isError, error, refetch } =
    useSystemSettings();

  // Keep the deployment mode exact. Mutation surfaces use writeAvailability separately so a
  // missing settings verdict blocks writes without falsely claiming that read-only mode is on.
  const isReadOnly = data?.read_only_mode ?? false;
  // important: the last good verdict answers until it is five minutes old. Deriving this from
  // isFetching or isError would flip every write control to "checking" on each minute's refetch
  // and to "unavailable" on one failed poll.
  const verdictIsFresh =
    data !== undefined && Date.now() - dataUpdatedAt < WRITE_VERDICT_MAX_AGE_MS;
  const writeAvailability: WriteAvailability = verdictIsFresh
    ? isReadOnly
      ? "read_only"
      : "available"
    : isLoading || isFetching
      ? "checking"
      : "unavailable";
  return {
    isReadOnly,
    isLoading,
    isFetching,
    isError,
    writeAvailability,
    writeUnavailableCause:
      writeAvailability === "unavailable" ? unavailableCause(error) : undefined,
    recheckWriteAvailability: refetch,
    apiVersion: data?.api_version,
  };
}

/**
 * Whether this deployment mounted the snapshot schedule routes.
 *
 * important: three states. `undefined` means not yet read, or read from a backend without the
 * field; a control then stays as it was rather than disabling itself on a page load. Only an
 * explicit `false` means the routes are not there.
 */
export function useSnapshotSchedulesEnabled(): boolean | undefined {
  return useSystemSettings().data?.snapshot_schedules_enabled;
}

/** Whether this deployment offers the file explorer, on the same three-state rule. */
export function useInstanceFilesEnabled(): boolean | undefined {
  return useSystemSettings().data?.instance_files_enabled;
}

/** Whether a Tenant with the Rebuild grant may rebuild here; see lib/rebuild-access.ts for its rule. */
export function useTenantRebuildEnabled(): boolean | undefined {
  return useSystemSettings().data?.tenant_rebuild_enabled;
}

/** Whether this deployment offers the guest credentials panel, on the same three-state rule. */
export function useGuestCredentialsEnabled(): boolean | undefined {
  return useSystemSettings().data?.guest_credentials_enabled;
}

/**
 * Whether this deployment mounted the task journal routes, on the same three-state rule: only
 * an explicit `false` hides the task panel.
 */
export function useTaskJournalEnabled(): boolean | undefined {
  return useSystemSettings().data?.task_journal_enabled;
}

/** Fail-closed: planning is offered only once the server said the Migration Center is on. */
function migrationPlanningUnavailableReason(
  isLoading: boolean,
  isError: boolean,
  centerEnabled: boolean | undefined,
): string | undefined {
  if (isLoading) return "Checking migration availability";
  if (isError) return "Migration availability could not be read";
  if (centerEnabled !== true) return "Migrations are turned off on this deployment";
  return undefined;
}

export function useMigrationCapabilities() {
  const query = useSystemSettings();
  // An error only matters while nothing was read. Last known capabilities stay valid.
  const isError = query.isError && !query.data;
  return {
    isLoading: query.isLoading,
    isError,
    error: isError ? query.error : null,
    refetch: query.refetch,
    planningUnavailableReason: migrationPlanningUnavailableReason(
      query.isLoading,
      isError,
      query.data?.migration_center_enabled,
    ),
    centerEnabled: query.data?.migration_center_enabled,
    recoveryActionsEnabled: query.data?.migration_recovery_actions_enabled,
    autoCleanupEnabled: query.data?.migration_auto_cleanup_enabled,
    runningSourceEnabled: query.data?.migration_running_source_enabled,
    defaultRetentionHours: query.data?.migration_default_retention_hours,
    durableCopyEnabled: query.data?.migration_durable_copy_enabled,
    instanceMoveEnabled: query.data?.migration_instance_move_enabled,
    projectMemberRelocationEnabled:
      query.data?.migration_project_member_relocation_enabled,
    projectCrossClusterEnabled:
      query.data?.migration_project_cross_cluster_enabled,
  };
}
