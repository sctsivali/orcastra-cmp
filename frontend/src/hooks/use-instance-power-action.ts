"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useActorQueryScope } from "@/hooks/use-query-scope";
import { api, isAuthActorSynchronized } from "@/lib/api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import type { VMActionResponse } from "@/types/api";

export type InstancePowerAction = "start" | "stop" | "restart";

export class ActorScopeChangedError extends Error {
  constructor() {
    super("The authenticated actor changed while the instance action was running");
    this.name = "ActorScopeChangedError";
  }
}

export function isActorScopeChangedError(error: unknown): error is ActorScopeChangedError {
  return error instanceof ActorScopeChangedError;
}

/** Run one power action, once, then reconcile every instance cache owned by the same actor. */
export function useInstancePowerAction() {
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();

  return React.useCallback(
    async (
      action: InstancePowerAction,
      target: { name: string; host: string; project: string; force?: boolean; timeout?: number },
    ): Promise<VMActionResponse> => {
      const actorScope = scopeKey;
      const request = {
        host: target.host,
        project: target.project,
        force: target.force ?? false,
        timeout: target.timeout ?? 30,
      };
      let result: VMActionResponse;
      try {
        if (action === "start") {
          result = await api.vms.start(target.name, request);
        } else if (action === "stop") {
          result = await api.vms.stop(target.name, request);
        } else {
          result = await api.vms.restart(target.name, request);
        }
      } catch (error) {
        if (!isAuthActorSynchronized(actorScope)) throw new ActorScopeChangedError();
        throw error;
      }

      if (!isAuthActorSynchronized(actorScope)) throw new ActorScopeChangedError();
      await reconcileMutationImpact(queryClient, "instance.state");
      if (!isAuthActorSynchronized(actorScope)) throw new ActorScopeChangedError();
      return result;
    },
    [queryClient, scopeKey],
  );
}
