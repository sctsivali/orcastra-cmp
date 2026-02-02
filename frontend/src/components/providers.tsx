"use client";

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "./theme-provider";
import { PersonalizationProvider } from "./personalization-provider";
import { StorageCleanupProvider } from "./storage-cleanup-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "./auth-provider";

function makeQueryClient() {
  return new QueryClient({
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
        // Retry failed requests once
        retry: 1,
        retryDelay: 1000,
      },
    },
  });
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

export function Providers({ children }: { children: React.ReactNode }) {
  const queryClient = getQueryClient();

  return (
    <AuthProvider>
      <QueryClientProvider client={queryClient}>
        <StorageCleanupProvider>
          <ThemeProvider defaultTheme="system" storageKey="orcastra-ui-theme">
            <PersonalizationProvider>
              <TooltipProvider delayDuration={150} skipDelayDuration={100}>
                {children}
              </TooltipProvider>
            </PersonalizationProvider>
          </ThemeProvider>
        </StorageCleanupProvider>
      </QueryClientProvider>
    </AuthProvider>
  );
}

