"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { IntentLink } from "@/components/navigation/route-intent";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { myKeysActivityAPI } from "@/lib/my-keys-activity-api";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { formatDate } from "@/lib/utils";
import type { KeyInfo } from "@/types/api";
import { KeyActivityLine } from "./key-activity-line";

const WINDOW_DAYS = 365;

/**
 * One key's activity: its dashboard trail and what was done with it on its cluster, newest first,
 * paged by the server. The full log, every key at once, is on /my-keys/activity.
 */
export function KeyActivityDialog({
  keyRow,
  onOpenChange,
}: {
  keyRow: (KeyInfo & { last_used_at: string | null }) | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { scopeKey } = useActorQueryScope();
  const [page, setPage] = useState(1);
  const [shown, setShown] = useState(keyRow);
  if (keyRow && keyRow !== shown) {
    setShown(keyRow);
    setPage(1);
  }
  const since = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString().split("T")[0];
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["my-key-activity", scopeKey, shown?.id, page],
    queryFn: ({ signal }) =>
      myKeysActivityAPI.list(
        { keyId: shown?.id, since, limit: PANEL_PAGE_SIZE, offset: (page - 1) * PANEL_PAGE_SIZE },
        { signal },
      ),
    enabled: keyRow !== null && Boolean(shown?.id),
    staleTime: 30_000,
  });
  const totalPages = Math.max(1, Math.ceil((data?.total ?? 0) / PANEL_PAGE_SIZE));

  return (
    <Dialog open={keyRow !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader className="pb-4">
          <DialogTitle>Activity for {shown?.name}</DialogTitle>
          <DialogDescription>
            What was done with this key in the last {WINDOW_DAYS} days: on the dashboard, and on {shown?.node} under its
            certificate. Last change on the cluster:{" "}
            {shown?.last_used_at ? formatDate(shown.last_used_at) : "not recorded"}. Reads such as a list leave no record.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-3" data-testid="key-activity-dialog">
          {isLoading ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              Loading activity…
            </div>
          ) : isError ? (
            <div className="space-y-2 py-6 text-sm">
              <p className="text-muted-foreground">The activity could not be read.</p>
              <Button size="sm" variant="outline" onClick={() => void refetch()}>
                Try Again
              </Button>
            </div>
          ) : !data || data.entries.length === 0 ? (
            <p className="py-6 text-sm text-muted-foreground">Nothing recorded for this key yet.</p>
          ) : (
            <ul className="divide-y rounded-md border">
              {data.entries.map((entry) => (
                <li key={entry.id} className="px-3 py-2">
                  <KeyActivityLine entry={entry} />
                </li>
              ))}
            </ul>
          )}
          {totalPages > 1 && (
            <DataTablePagination
              currentPage={Math.min(page, totalPages)}
              totalPages={totalPages}
              pageSize={PANEL_PAGE_SIZE}
              pageSizeOptions={[PANEL_PAGE_SIZE]}
              totalItems={data?.total ?? 0}
              onPageChange={setPage}
              onPageSizeChange={() => undefined}
            />
          )}
          <p className="text-sm">
            <IntentLink href={`/my-keys/activity?key=${encodeURIComponent(shown?.id ?? "")}`} className="underline decoration-dotted underline-offset-4 hover:decoration-solid">
              Open the full activity log
            </IntentLink>
          </p>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
