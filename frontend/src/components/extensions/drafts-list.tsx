"use client";

import { useState } from "react";
import { RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import type { ExtensionDetail } from "@/types/api";

const PAGE_SIZE = 10;

interface DraftsListProps {
  rows: ExtensionDetail[];
  loading: boolean;
  /** The catalogue read these come from. A failure here is not an author with no drafts. */
  error: Error | null;
  onRetry: () => void;
  onPublish: (slug: string) => void;
  onDiscard: (slug: string) => void;
  publishPending: boolean;
  discardPending: boolean;
}

/**
 * Drafts an author has not published yet.
 *
 * The section used to be gated on there being at least one, so it had no loading, empty or error
 * state and simply popped into existence. An author who had just discarded their last draft got no
 * confirmation that the list was now empty rather than still loading.
 *
 * A feed rather than a table: there is nothing here to filter by, and the table contract requires
 * every visible column to be filterable. It takes a pager because an author working through a
 * backlog is the one person who will have many.
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

  // Same distinction the other two lists make: telling an author they have no drafts when the read
  // failed is a wrong statement, and it is the reassuring kind.
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

  // The caller does not render this section with nothing to show, so this is a guard rather than a
  // state an operator reaches. An empty list here would paint a bordered box around no rows.
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
                for {extension.requested_capabilities.length} capabilit
                {extension.requested_capabilities.length === 1 ? "y" : "ies"}
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
