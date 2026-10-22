"use client";

import * as React from "react";
import { ChunkReloadReset } from "@/components/chunk-reload-reset";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { ThemeProvider } from "./theme-provider";
import { PersonalizationProvider } from "./personalization-provider";
import { StorageCleanupProvider } from "./storage-cleanup-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "./auth-provider";
import { NavigationProgressProvider } from "./navigation/navigation-progress";
import { RouteIntentProvider } from "./navigation/route-intent";
import { APIError } from "@/types";
import { POLL_MS } from "@/lib/query-config";
import { onAuthActorChanged, onTokenRefreshed } from "@/lib/api";
import { registerExternalMutationQueryClient } from "@/lib/mutation-coherence";
import type { PersonalizationSettings } from "@/types/personalization";
import type { Session } from "next-auth";

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
      // important: only the queries that actually failed under the old token.
      // A bare invalidateQueries() refetches the entire cache on every rotation,
      // which fires N concurrent requests, reads to the operator as a page
      // reload, and manufactures the 401 burst that used to redirect them away.
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

function makeQueryClient() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        // Increase staleTime to reduce refetching
        staleTime: 60 * 1000, // 1 minute (data is considered fresh for 1 minute)
        // Cache data for 5 minutes
        gcTime: 5 * 60 * 1000, // 5 minutes (formerly cacheTime in v4)
        // Disable aggressive refetching
        refetchOnWindowFocus: false, // Don't refetch when switching tabs
        refetchOnMount: false, // Don't refetch on component mount if data is fresh
        refetchOnReconnect: true, // Still refetch on reconnect
        // Smart retry: skip 401/403/429 (won't resolve by retrying)
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
  // Three pages subscribed to ["nodes"] with no options at all, so whether their numbers moved
  // depended on the dashboard being open in the same tab session. Those call sites now go
  // through the owning hooks; this is the net underneath, so a future one that forgets still
  // polls. Observer options always win over these, so it cannot fight an explicit hook.
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
    // Server: always make a new query client
    return makeQueryClient();
  } else {
    // Browser: make a new query client if we don't already have one
    if (!browserQueryClient) browserQueryClient = makeQueryClient();
    return browserQueryClient;
  }
}

export function Providers({
  children,
  initialPersonalization,
  initialSession,
}: {
  children: React.ReactNode;
  // Read from the hint cookie by the root layout, which is the only server component in this
  // tree, and handed down so the server and the browser render the same sidebar on the first
  // pass. See the note on PersonalizationProvider's initialSettings prop.
  initialPersonalization?: PersonalizationSettings | null;
  // A comfortably valid session read by the root layout (auth-session.ts), or undefined to let
  // the browser fetch one as before. Never null.
  initialSession?: Session;
}) {
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
    <AuthProvider session={initialSession}>
      <QueryClientProvider client={queryClient}>
        <QueryInvalidationBridge />
        <ChunkReloadReset />
        <StorageCleanupProvider>
          <PersonalizationProvider initialSettings={initialPersonalization}>
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
