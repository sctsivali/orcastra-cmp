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
  /** Cancels transport work when the observer or query key is retired. */
  signal: AbortSignal;
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

export interface RefreshIntentTracker {
  isFresh: () => boolean;
  acquire: () => () => void;
}

/** Reference-count overlapping manual refreshes so one completion cannot retire another's intent. */
export function createRefreshIntentTracker(): RefreshIntentTracker {
  let active = 0;
  return {
    isFresh: () => active > 0,
    acquire: () => {
      active += 1;
      let released = false;
      return () => {
        if (released) return;
        released = true;
        active = Math.max(0, active - 1);
      };
    },
  };
}

/**
 * A useQuery whose queryFn knows whether this fetch came from a person clicking
 * refresh or from background polling.
 *
 * important: React Query's refetch() takes no arguments, so the "bypass the
 * server cache" intent travels in a ref, raised only for one manual run.
 * Background polling keeps reading the cached path, so many open tabs do not
 * turn into a fan-out generator against live clusters.
 */
export function useRefreshableQuery<TData, TError = Error>(
  options: RefreshableQueryOptions<TData, TError>,
): RefreshableQueryResult<TData, TError> {
  const { queryFn, ...rest } = options;
  const intentRef = useRef<RefreshIntentTracker | null>(null);
  if (!intentRef.current) intentRef.current = createRefreshIntentTracker();

  const query = useQuery({
    ...rest,
    // important: read, never consumed. The flag stays raised for the whole manual run, retries
    // included: a retry is still part of the refresh the operator asked for. Cleared on first
    // read, the retry after a failed bypass would go out without fresh=true, hit the cache and
    // report success. A poll tick mid-run sends no second request: React Query dedupes it
    // against the in-flight fetch for the same key.
    queryFn: ({ signal }) => queryFn({ fresh: intentRef.current?.isFresh() === true, signal }),
  } as UseQueryOptions<TData, TError, TData>) as UseQueryResult<TData, TError>;

  const { refetch } = query;

  const refreshNow = useCallback(async () => {
    const release = intentRef.current?.acquire() ?? (() => undefined);
    try {
      // throwOnError so a failed refresh rejects here instead of being absorbed
      // into query.error, which the call sites do not read.
      await refetch({ throwOnError: true });
    } finally {
      release();
    }
  }, [refetch]);

  // Spreading the result opts out of React Query's tracked-properties
  // optimisation. These are page-level list queries whose consumers already read
  // data, isLoading, isError and dataUpdatedAt, so nothing measurable is lost.
  return { ...query, refreshNow };
}
