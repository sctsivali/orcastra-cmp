"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Search as SearchIcon } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useDebouncedValue } from "@/hooks/use-debounced-value";
import { MIN_SEARCH_LEN, useGlobalSearch } from "@/hooks/use-global-search";

// Full results page: the "view all" target from the command palette. Reuses the
// same RBAC-scoped backend search, just with a higher per-type limit. Reads the
// initial query from the URL on mount (client-only, avoids a Suspense boundary)
// and keeps the URL in sync so a search is shareable.
export default function SearchPage() {
  const router = useRouter();
  const [query, setQuery] = React.useState("");

  React.useEffect(() => {
    const initial = new URLSearchParams(window.location.search).get("q");
    if (initial) setQuery(initial);
  }, []);

  const debounced = useDebouncedValue(query, 250);
  const { data, isFetching } = useGlobalSearch(debounced, { limit: 25 });

  React.useEffect(() => {
    const trimmed = debounced.trim();
    const qs = trimmed ? `?q=${encodeURIComponent(trimmed)}` : "";
    window.history.replaceState(null, "", `/search${qs}`);
  }, [debounced]);

  const trimmed = debounced.trim();
  const groups = data?.groups ?? [];

  return (
    <div className="container space-y-6 px-4 py-6 md:px-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-semibold">Search</h1>
        <p className="text-sm text-muted-foreground">
          Find instances, projects, networks, storage, images, profiles and more.
        </p>
      </div>

      <div className="relative max-w-xl">
        <SearchIcon className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search..."
          className="pl-9"
        />
      </div>

      {data?.degraded && (
        <p className="text-xs text-amber-600 dark:text-amber-500">
          Some sources were unavailable, so results may be incomplete.
        </p>
      )}

      {trimmed.length < MIN_SEARCH_LEN ? (
        <div className="rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
          Type at least {MIN_SEARCH_LEN} characters to search.
        </div>
      ) : groups.length === 0 && !isFetching ? (
        <div className="rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
          No results for &ldquo;{trimmed}&rdquo;.
        </div>
      ) : (
        <div className="space-y-6">
          {groups.map((group) => (
            <Card key={group.type}>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-2 text-base">
                  {group.label}
                  <Badge variant="secondary">{group.total}</Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="divide-y p-2">
                {group.items.map((item) => (
                  <button
                    key={`${item.type}:${item.id}`}
                    onClick={() => router.push(item.href)}
                    className="flex w-full items-center justify-between gap-3 rounded px-2 py-2 text-left hover:bg-muted/50"
                  >
                    <div className="min-w-0">
                      <div className="truncate font-medium">{item.title}</div>
                      {item.subtitle && (
                        <div className="truncate text-xs text-muted-foreground">{item.subtitle}</div>
                      )}
                    </div>
                    {item.match_field && (
                      <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
                        {item.match_field}
                      </span>
                    )}
                  </button>
                ))}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
