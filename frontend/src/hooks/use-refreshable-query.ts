"use client";

import { useCallback, useRef } from "react";
import {
  useQuery,
  type UseQueryOptions,
  type UseQueryResult,
} from "@tanstack/react-query";

export interface RefreshableQueryContext {
  /** True only for a fetch started by an explicit user action. */
  fresh: boolean;
}

export type RefreshableQueryOptions<TData, TError = Error> = Omit<
  UseQueryOptions<TData, TError, TData>,
  "queryFn"
> & {
  queryFn: (ctx: RefreshableQueryContext) => Promise<TData>;
};

export type RefreshableQueryResult<TData, TError = Error> = UseQueryResult<
  TData,
  TError
> & {
  /** Fetch bypassing the server response cache. Rejects so callers can report failure. */
  refreshNow: () => Promise<void>;
};

/**
 * A useQuery whose queryFn knows whether this particular fetch came from a
 * person clicking refresh or from background polling.
 *
 * important: React Query's refetch() takes no arguments, so the "bypass the
 * server cache" intent cannot be passed through it. A ref carries the intent
 * instead, set only for the duration of one manual run. Background polling
 * keeps reading the cached path, which is what stops a room full of open tabs
 * from turning into a fan-out generator against live clusters.
 */
export function useRefreshableQuery<TData, TError = Error>(
  options: RefreshableQueryOptions<TData, TError>,
): RefreshableQueryResult<TData, TError> {
  const { queryFn, ...rest } = options;
  const freshRef = useRef(false);

  const query = useQuery({
    ...rest,
    // important: read, never consumed. The flag has to stay raised for the whole manual run,
    // retries included, because a retry is still part of the refresh the operator asked for.
    //
    // This was briefly changed to clear the flag on first read, to stop a poll tick landing
    // mid-run from also sending fresh=true. That concern was wrong twice over. React Query
    // already dedupes a tick against an in-flight fetch for the same key, so no second request
    // was possible. And clearing it meant the retry after a failed bypass went out WITHOUT
    // fresh=true, hit the cache, succeeded, and the button reported success: a forced refresh
    // silently degrading into a cached read, which is the exact defect this whole hook exists
    // to prevent. Caught by refresh-affordances.spec.ts "a failed refresh reports failure".
    queryFn: () => queryFn({ fresh: freshRef.current }),
  } as UseQueryOptions<TData, TError, TData>) as UseQueryResult<TData, TError>;

  const { refetch } = query;

  const refreshNow = useCallback(async () => {
    freshRef.current = true;
    try {
      // throwOnError so a failed refresh rejects here instead of being absorbed
      // into query.error, where every current call site ignores it
      await refetch({ throwOnError: true });
    } finally {
      freshRef.current = false;
    }
  }, [refetch]);

  // note: spreading the result opts out of React Query's tracked-properties
  // optimisation. These are page-level list queries whose consumers already read
  // data, isLoading, isError and dataUpdatedAt, so nothing measurable is lost.
  return { ...query, refreshNow };
}
