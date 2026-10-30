"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useInstanceFilesEnabled } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { captureActorMutationFence, dispatchActorMutation } from "@/lib/actor-mutation-fence";
import { fileErrorCode, instanceFilesAPI, type DirectoryListing, type GuestTarget, type OnConflict } from "@/lib/instance-files/api";
import { directoryKey, fileContentKey } from "@/lib/instance-files/keys";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";

const LISTING_STALE_MS = 5_000;
const LISTING_GC_MS = 60_000;

// A refusal is an answer: asking a stopped instance again in a second tells nobody anything.
function retryListing(failures: number, error: unknown): boolean {
  return fileErrorCode(error) === undefined && failures < 1;
}

export function useDirectory(target: GuestTarget, path: string, enabled: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery<DirectoryListing>({
    queryKey: directoryKey(scopeKey, target, path),
    queryFn: ({ signal }) => instanceFilesAPI.list(target, path, signal),
    enabled: enabled && isAuthenticated,
    staleTime: LISTING_STALE_MS,
    gcTime: LISTING_GC_MS,
    retry: retryListing,
  });
}

/** Why this person cannot reach this instance's files, or undefined when they can. */
export function useFilesUnavailableReason(host: string, project: string, instanceName: string): string | undefined {
  const enabled = useInstanceFilesEnabled();
  const { hasPermission } = useTenantAccess();
  if (enabled === false) return "File access is turned off on this deployment";
  return hasPermission(host, project, instanceName, "file_transfer")
    ? undefined
    : "Your access to this instance does not include files";
}

/** The writes of one explorer, each dispatched under the actor that asked for it. */
export function useFileActions(target: GuestTarget) {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();

  const entryChange = useCallback(
    async <T,>(write: () => Promise<T>): Promise<T | undefined> => {
      const outcome = await dispatchActorMutation(captureActorMutationFence(scopeKey), write);
      if (outcome.retired) return undefined;
      await reconcileMutationImpact(queryClient, "instance-files.entry");
      return outcome.value;
    },
    [queryClient, scopeKey],
  );

  return useMemo(
    () => ({
      createFolder: (path: string) =>
        entryChange(async () => {
          const created = await instanceFilesAPI.createFolder(target, path);
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return created;
        }),
      createFile: (path: string) =>
        entryChange(async () => {
          const created = await instanceFilesAPI.createFile(target, path);
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return created;
        }),
      rename: (source: string, destination: string, onConflict: OnConflict = "refuse") =>
        entryChange(async () => {
          const moved = await instanceFilesAPI.rename(target, source, destination, onConflict);
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return moved;
        }),
      setMode: (path: string, mode: number, recursive: boolean) =>
        entryChange(async () => {
          let result = await instanceFilesAPI.setMode(target, path, mode, recursive);
          // Each slice skips what already matches, so this ends when a slice has nothing to do.
          while (result.remaining && result.changed > 0) {
            result = await instanceFilesAPI.setMode(target, path, mode, recursive);
          }
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return result;
        }),
      setOwner: (path: string, uid: number, gid: number, recursive: boolean) =>
        entryChange(async () => {
          let result = await instanceFilesAPI.setOwner(target, path, uid, gid, recursive);
          while (result.remaining && result.changed > 0) {
            result = await instanceFilesAPI.setOwner(target, path, uid, gid, recursive);
          }
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return result;
        }),
      remove: (path: string, recursive: boolean, onProgress?: (deleted: number) => void) =>
        entryChange(async () => {
          let deleted = 0;
          const failed: string[] = [];
          let failedCount = 0;
          for (;;) {
            const slice = await instanceFilesAPI.delete(target, path, recursive);
            deleted += slice.deleted;
            failedCount += slice.failed_count;
            failed.push(...slice.failed);
            onProgress?.(deleted);
            if (!slice.remaining) break;
          }
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return { deleted, failed: [...new Set(failed)], failedCount };
        }),
      purgeUploads: (path: string) =>
        entryChange(async () => {
          const purged = await instanceFilesAPI.purgeUploads(target, path);
          await reconcileMutationImpact(queryClient, "instance-files.entry");
          return purged;
        }),
      save: async (path: string, text: string, etag: string, allowInPlace = false) => {
        const outcome = await dispatchActorMutation(captureActorMutationFence(scopeKey), () =>
          instanceFilesAPI.save(target, path, text, etag, allowInPlace),
        );
        if (outcome.retired) return undefined;
        await reconcileMutationImpact(queryClient, "instance-files.content", {
          queryKeys: [fileContentKey(scopeKey, target, path)],
        });
        return outcome.value;
      },
    }),
    [entryChange, queryClient, scopeKey, target],
  );
}
