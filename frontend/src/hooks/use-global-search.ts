import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { SearchResponse } from "@/types";

// Minimum query length before we hit the backend. Short queries match too much
// and waste a fan-out; the UI shows a "keep typing" hint below this.
export const MIN_SEARCH_LEN = 2;

// React Query wrapper around the global search endpoint. keepPreviousData keeps
// the last results visible while the next query is in flight (no flicker).
export function useGlobalSearch(query: string, opts?: { types?: string[]; limit?: number }) {
  const q = query.trim();
  return useQuery<SearchResponse>({
    queryKey: ["search", q, opts?.types ?? null, opts?.limit ?? null],
    queryFn: () => api.search.global(q, opts),
    enabled: q.length >= MIN_SEARCH_LEN,
    staleTime: 15_000,
    gcTime: 60_000,
    placeholderData: keepPreviousData,
    retry: false,
  });
}
