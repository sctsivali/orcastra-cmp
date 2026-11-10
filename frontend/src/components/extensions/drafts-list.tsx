"use client";

import { useState } from "react";
import { RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import type { ExtensionDetail } from "@/types/api";
import { plural } from "@/lib/utils";

const PAGE_SIZE = 10;

interface DraftsListProps {
  rows: ExtensionDetail[];
  loading: boolean;
  /** Error from the catalogue read these come from; shown as an error, never as no drafts. */
  error: Error | null;
  onRetry: () => void;
  onPublish: (slug: string) => void;
  onDiscard: (slug: string) => void;
  publishPending: boolean;
  discardPending: boolean;
}

/**
 * Drafts an author has not published yet, with loading and error states.
 *
 * A feed rather than a table: there is nothing here to filter by, and the table contract requires
 * every visible column to be filterable. Paged, because an author with a backlog can have many.
 */
export function DraftsList({
  rows,
  loading,
  error,
  onRetry,
  onPublish,
  onDiscard,
  publishPending,
  discardPending,
}: DraftsListProps) {
  const [page, setPage] = useState(1);

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const paginated = rows.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  if (loading) return <Skeleton className="h-24 rounded-lg" />;

  // A failed read renders an error with Retry rather than claiming the author has no drafts.
  if (error) {
    return (
      <EmptyState
        icon={RotateCcw}
        title="Could not load your drafts"
        description={error.message}
        action={
          <Button size="sm" onClick={onRetry}>
            Retry
          </Button>
        }
      />
    );
  }

  // A guard: the caller hides this section when there is nothing to show. An empty list here would
  // paint a bordered box around no rows.
  if (rows.length === 0) return null;

  return (
    <div className="space-y-4">
      <ul className="divide-y rounded-lg border">
        {paginated.map((extension) => (
          <li key={extension.slug} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
            <div className="min-w-0 flex-1">
              <p className="font-medium">{extension.name}</p>
              <p className="text-xs text-muted-foreground">
                <span className="font-mono">{extension.slug}</span> · v{extension.version} · asks
                for {plural(extension.requested_capabilities.length, "capability", "capabilities")}
              </p>
            </div>
            <Badge variant="caution">Draft</Badge>
            <Button
              size="sm"
              disabled={publishPending}
              onClick={() => onPublish(extension.slug)}
            >
              Publish
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={discardPending}
              onClick={() => onDiscard(extension.slug)}
            >
              Discard
            </Button>
          </li>
        ))}
      </ul>

      {totalPages > 1 && (
        <DataTablePagination
          variant="compact"
          currentPage={currentPage}
          totalPages={totalPages}
          onPageChange={setPage}
          label="your unpublished drafts"
        />
      )}
    </div>
  );
}
