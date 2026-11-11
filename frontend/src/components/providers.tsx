"use client";

import * as React from "react";
import { ChunkReloadReset } from "@/components/chunk-reload-reset";
import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";
import { ThemeProvider } from "./theme-provider";
import { PersonalizationProvider } from "./personalization-provider";
import { StorageCleanupProvider } from "./storage-cleanup-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "./auth-provider";
import { NavigationProgressProvider } from "./navigation/navigation-progress";
import { RouteIntentProvider } from "./navigation/route-intent";
import { APIError } from "@/types";
import { POLL_MS } from "@/lib/query-config";
import {
  STALE_FOLLOW_UP_DELAYS_MS,
  onAuthActorChanged,
  onTokenRefreshed,
  wasServedStale,
} from "@/lib/api";
import { registerExternalMutationQueryClient } from "@/lib/mutation-coherence";
import { PendingCreateFollower } from "@/components/pending-create-follower";
import type { PersonalizationSettings } from "@/types/personalization";
import type { ByteUnitSystem } from "@/lib/byte-units";
import { unmaskSession, type HydratedSession } from "@/lib/hydrated-session";

/**
 * Refetches queries that errored under an expired token once the access token
 * is silently refreshed. Lives inside QueryClientProvider so it can reach the
 * client; api.ts fires onTokenRefreshed when the Bearer token rotates.
 */
function QueryInvalidationBridge() {
  const queryClient = useQueryClient();
  React.useEffect(
    () => registerExternalMutationQueryClient(queryClient),
    [queryClient],
  );
  React.useEffect(() => {
    onTokenRefreshed(() => {
      // important: only the queries that failed with a 401 under the previous token.
      // A bare invalidateQueries() would refetch the entire cache on every
      // rotation: N concurrent requests that read to the operator as a reload.
      queryClient.invalidateQueries({
        predicate: (query) =>
          query.state.status === "error" &&
          query.state.error instanceof APIError &&
          query.state.error.status === 401,
        refetchType: "active",
      });
    });
  }, [queryClient]);
  return null;
}

/**
 * Ask again once the backend has replaced a stale answer (wasServedStale in lib/api.ts).
 *
 * At most STALE_FOLLOW_UP_DELAYS_MS.length looks per query while its answers stay stale, only
 * for a query something is still watching, and only while the page is visible: a hidden tab
 * catches up on its own poll.
 */
function convergeOnFreshData(getClient: () => QueryClient | undefined) {
  const followUps = new Map<string, number>();
  return (data: unknown, query: Query<unknown, unknown, unknown, readonly unknown[]>) => {
    if (!wasServedStale(data)) {
      followUps.delete(query.queryHash);
      return;
    }
    const attempt = followUps.get(query.queryHash) ?? 0;
    const delay = STALE_FOLLOW_UP_DELAYS_MS[attempt];
    if (delay === undefined) return;
    followUps.set(query.queryHash, attempt + 1);
    setTimeout(() => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      const client = getClient();
      if (!client || client.getQueryCache().get(query.queryHash) !== query) return;
      void client.refetchQueries({ queryKey: query.queryKey, exact: true, type: "active" });
    }, delay);
  };
}

function makeQueryClient() {
  let client: QueryClient | undefined;
  const onStaleAnswer = convergeOnFreshData(() => client);
  client = new QueryClient({
    queryCache: new QueryCache({
      onSuccess: (data, query) => onStaleAnswer(data, query),
    }),
    defaultOptions: {
      queries: {
        staleTime: 60 * 1000,
        gcTime: 5 * 60 * 1000,
        refetchOnWindowFocus: false,
        // important: true refetches on mount only when the data is stale or was invalidated.
        // Turning it off would skip the refetch even after a mutation invalidated the query, so
        // a list opened on another page would show what a write just changed until a reload.
        refetchOnMount: true,
        refetchOnReconnect: true,
        // No retry on 401, 403 or 429: retrying cannot change the answer.
        retry: (failureCount, error) => {
          if (error instanceof APIError) {
            if (error.status === 401 || error.status === 429 || error.status === 403) {
              return false;
            }
          }
          return failureCount < 1;
        },
        retryDelay: 1000,
      },
    },
  });

  // important: liveness belongs to the key, not to whichever component happens to be mounted.
  // A subscriber that passes no options still polls through these defaults. Observer options
  // always win over them, so they cannot fight an explicit hook.
  client.setQueryDefaults(["nodes"], {
    refetchInterval: POLL_MS.fleet,
    refetchIntervalInBackground: false,
  });
  client.setQueryDefaults(["instances"], {
    refetchInterval: POLL_MS.active,
    refetchIntervalInBackground: false,
  });

  return client;
}

let browserQueryClient: QueryClient | undefined = undefined;

function getQueryClient() {
  if (typeof window === "undefined") {
    // Server: a new client per request, so no cache is shared between requests.
    return makeQueryClient();
  } else {
    // Browser: one client, so a render that suspends does not drop the cache.
    if (!browserQueryClient) browserQueryClient = makeQueryClient();
    return browserQueryClient;
  }
}

export function Providers({
  children,
  initialPersonalization,
  initialByteUnits,
  initialSession,
}: {
  children: React.ReactNode;
  // Read from the hint cookie by the root layout, which is the only server component in this
  // tree, and handed down so the server and the browser render the same sidebar on the first
  // pass.
  initialPersonalization?: PersonalizationSettings | null;
  initialByteUnits?: ByteUnitSystem;
  // A comfortably valid session read by the root layout (auth-session.ts), its token masked, or
  // undefined to let the browser fetch one. Never null.
  initialSession?: HydratedSession;
}) {
  // Unmasked in memory only; the document carries the masked form (lib/hydrated-session.ts).
  const session = React.useMemo(() => unmaskSession(initialSession), [initialSession]);
  const [queryClient, setQueryClient] = React.useState(getQueryClient);
  const queryClientRef = React.useRef(queryClient);

  React.useEffect(
    () =>
      onAuthActorChanged(() => {
        // TokenSync is withholding the descendant tree while this callback runs. Retire the
        // complete client, not a hand-maintained list of sensitive keys: one overlooked query
        // would otherwise let B render A's last-good body from the singleton browser cache.
        const previous = queryClientRef.current;
        const next = makeQueryClient();
        if (typeof window !== "undefined") browserQueryClient = next;
        queryClientRef.current = next;
        previous.clear();
        setQueryClient(next);
      }),
    [],
  );

  return (
    <AuthProvider session={session}>
      <QueryClientProvider client={queryClient}>
        <QueryInvalidationBridge />
        <PendingCreateFollower />
        <ChunkReloadReset />
        <StorageCleanupProvider>
          <PersonalizationProvider initialSettings={initialPersonalization} initialByteUnits={initialByteUnits}>
            <ThemeProvider>
              <TooltipProvider delayDuration={150} skipDelayDuration={100}>
                <NavigationProgressProvider>
                  <RouteIntentProvider>{children}</RouteIntentProvider>
                </NavigationProgressProvider>
              </TooltipProvider>
            </ThemeProvider>
          </PersonalizationProvider>
        </StorageCleanupProvider>
      </QueryClientProvider>
    </AuthProvider>
  );
}
