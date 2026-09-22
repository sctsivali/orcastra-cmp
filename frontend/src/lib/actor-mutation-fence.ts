import {
  getAuthActorGeneration,
  isAuthActorSynchronized,
} from "@/lib/api";

export type ActorMutationFence = Readonly<{
  scopeKey: string;
  actorGeneration: number;
}>;

export function captureActorMutationFence(scopeKey: string): ActorMutationFence {
  return {
    scopeKey,
    actorGeneration: getAuthActorGeneration(),
  };
}

export function isActorMutationFenceCurrent(fence: ActorMutationFence): boolean {
  return (
    getAuthActorGeneration() === fence.actorGeneration &&
    isAuthActorSynchronized(fence.scopeKey)
  );
}

/** Dispatch only while the captured actor epoch still owns the mutation. */
export async function dispatchActorMutation<T>(
  fence: ActorMutationFence,
  mutation: () => Promise<T>,
): Promise<{ retired: true } | { retired: false; value: T }> {
  if (!isActorMutationFenceCurrent(fence)) return { retired: true };
  const value = await mutation();
  if (!isActorMutationFenceCurrent(fence)) return { retired: true };
  return { retired: false, value };
}
