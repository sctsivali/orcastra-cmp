"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useGuestCredentialsEnabled, useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { captureActorMutationFence, dispatchActorMutation } from "@/lib/actor-mutation-fence";
import {
  changeMayHaveLanded,
  credentialErrorCode,
  guestCredentialsAPI,
  type CreateUserBody,
  type CreateUserResult,
  type GuestOverview,
  type GuestTarget,
  type KeyAddResult,
  type KeyList,
  type PasswordResult,
  type SshServerFacts,
  type SudoResult,
} from "@/lib/guest-credentials/api";
import { credentialsKey } from "@/lib/guest-credentials/keys";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { APIError } from "@/types/api";

const STALE_MS = 15_000;
const GC_MS = 60_000;

// A refusal is an answer: asking a stopped instance again a second later tells nobody anything,
// and every read spends the person's per-minute budget on the server.
function retryRead(failures: number, error: unknown): boolean {
  if (credentialErrorCode(error) !== undefined) return false;
  if (error instanceof APIError && error.status >= 400 && error.status < 500) return false;
  return failures < 1;
}

// note: no polling and no refetch on focus. Each read runs a script inside the guest, so the
// panel reads when it is opened, after a change, and when the person asks.
const READ_OPTIONS = { staleTime: STALE_MS, gcTime: GC_MS, retry: retryRead, refetchOnWindowFocus: false } as const;

export function useGuestOverview(target: GuestTarget, enabled: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery<GuestOverview>({
    queryKey: credentialsKey(scopeKey, target, "overview"),
    queryFn: ({ signal }) => guestCredentialsAPI.overview(target, signal),
    enabled: enabled && isAuthenticated,
    ...READ_OPTIONS,
  });
}

export function useSshServer(target: GuestTarget, enabled: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery<SshServerFacts>({
    queryKey: credentialsKey(scopeKey, target, "ssh"),
    queryFn: ({ signal }) => guestCredentialsAPI.sshServer(target, signal),
    enabled: enabled && isAuthenticated,
    ...READ_OPTIONS,
  });
}

export function useAuthorizedKeys(target: GuestTarget, user: string | null, enabled: boolean) {
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  return useQuery<KeyList>({
    queryKey: credentialsKey(scopeKey, target, "keys", user ?? ""),
    queryFn: ({ signal }) => guestCredentialsAPI.listKeys(target, user ?? "", signal),
    enabled: enabled && isAuthenticated && !!user,
    ...READ_OPTIONS,
  });
}

/** Why this person cannot open the Credentials panel, or undefined when they can. */
export function useCredentialsUnavailableReason(host: string, project: string, instanceName: string): string | undefined {
  const enabled = useGuestCredentialsEnabled();
  const { hasPermission } = useTenantAccess();
  if (enabled === false) return "Credentials are turned off on this deployment";
  // The same permission as a shell: setting root's password is at least as strong as one.
  return hasPermission(host, project, instanceName, "console")
    ? undefined
    : "Your access to this instance does not include the console";
}

/** Why changes are not offered right now, or undefined when they are. */
export function useCredentialsWriteReason(host: string, project: string, instanceName: string): string | undefined {
  const { termReadOnlyReason } = useTenantAccess();
  const { writeAvailability, writeUnavailableCause } = useReadOnlyMode();
  const termReason = termReadOnlyReason(host, project, instanceName);
  if (termReason) return termReason;
  if (writeAvailability === "read_only") return "This deployment is in read-only mode";
  if (writeAvailability === "checking") return "Checking whether this deployment accepts changes";
  if (writeAvailability === "unavailable") return writeUnavailableCause;
  return undefined;
}

export type CredentialChange =
  | { kind: "add-key"; user: string; publicKey: string }
  | { kind: "remove-key"; user: string; fingerprint: string }
  | { kind: "set-password"; user: string; password: string; mustChange: boolean }
  | { kind: "lock-password"; user: string }
  | { kind: "sudo"; user: string; enabled: boolean }
  | { kind: "create-user"; body: CreateUserBody };

interface ChangeResults {
  "add-key": KeyAddResult;
  "remove-key": { removed: number };
  "set-password": PasswordResult;
  "lock-password": PasswordResult;
  sudo: SudoResult;
  "create-user": CreateUserResult;
}

type ChangeKind = CredentialChange["kind"];

/**
 * One change to one guest, sent under the actor that asked for it. Resolves undefined when the
 * actor changed on the way, so a dialog never reports another person's answer.
 *
 * important: no useMutation. Its cache keeps the variables of the last call, and here those are a
 * password or a pasted key. The secret lives only in the caller's state and in this call's closure.
 */
export function useCredentialChange(target: GuestTarget) {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  return useCallback(
    async <K extends ChangeKind>(change: Extract<CredentialChange, { kind: K }>): Promise<ChangeResults[K] | undefined> => {
      let landed = true;
      try {
        const outcome = await dispatchActorMutation(captureActorMutationFence(scopeKey), (): Promise<ChangeResults[ChangeKind]> => {
          const sent = change as CredentialChange;
          switch (sent.kind) {
            case "add-key": return guestCredentialsAPI.addKey(target, sent.user, sent.publicKey);
            case "remove-key": return guestCredentialsAPI.removeKey(target, sent.user, sent.fingerprint);
            case "set-password": return guestCredentialsAPI.setPassword(target, sent.user, sent.password, sent.mustChange);
            case "lock-password": return guestCredentialsAPI.lockPassword(target, sent.user);
            case "sudo": return guestCredentialsAPI.setSudo(target, sent.user, sent.enabled);
            case "create-user": return guestCredentialsAPI.createUser(target, sent.body);
          }
        });
        return outcome.retired ? undefined : (outcome.value as ChangeResults[K]);
      } catch (error) {
        landed = changeMayHaveLanded(error);
        throw error;
      } finally {
        // Not awaited: the dialog reports the answer at once and the lists catch up behind it.
        if (landed) void reconcileMutationImpact(queryClient, "guest-credentials.change").catch(() => undefined);
      }
    },
    [queryClient, scopeKey, target],
  );
}
