"use client";

import { Suspense, useState } from "react";
import { Edit3, FileClock, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DestructiveConfirmDialog } from "@/components/ui/destructive-confirm-dialog";
import { TableToolbar } from "@/components/ui/table-toolbar";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { useDiscardMigrationPlan } from "@/hooks/use-migrations";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatDateTime, formatLabel, plural } from "@/lib/utils";
import type { MigrationPlan } from "@/types/migrations";
import { endpointLabel, endpointPrimary } from "./migration-format";

const MIGRATION_DRAFTS = defineTable<MigrationPlan>()({
  id: "migration-draft",
  searchPlaceholder: "Search migration drafts",
  defaultSort: { field: "expires", direction: "asc" },
  searchExtra: (plan) => [plan.plan_id],
  columns: [
    {
      field: "source",
      label: "Source",
      sort: (plan) => endpointLabel(plan.source),
      search: (plan) => endpointLabel(plan.source),
      filter: fromSort("text"),
    },
    {
      field: "destination",
      label: "Destination",
      sort: (plan) => endpointLabel(plan.target),
      search: (plan) => endpointLabel(plan.target),
      filter: fromSort("text"),
    },
    {
      field: "operation",
      label: "Operation",
      sort: (plan) => `${plan.intent} ${plan.scope}`,
      facet: (plan) => `${plan.intent} ${plan.scope}`,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "status",
      label: "Status",
      sort: (plan) => plan.status,
      facet: (plan) => plan.status,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "expires",
      label: "Expires",
      sort: (plan) => Date.parse(plan.expires_at),
      filter: fromSort("date", { horizon: "future" }),
    },
  ],
});

function draftStatusLabel(plan: MigrationPlan, expired: boolean): string {
  if (expired || plan.status === "expired") return "Expired";
  if (plan.status === "preflighting") return "Preflighting";
  if (plan.status === "blocked" || plan.blockers?.length) {
    const blockers = plan.blockers?.length ?? 0;
    return blockers ? plural(blockers, "Blocker") : "Blocked";
  }
  if (plan.status === "ready") return "Ready";
  return "Draft";
}

export function MigrationDraftTable({
  plans,
  onOpen,
}: {
  plans: MigrationPlan[];
  onOpen: (planId: string) => void;
}) {
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground" role="status">Loading draft filters…</p>}>
      <MigrationDraftTableContent plans={plans} onOpen={onOpen} />
    </Suspense>
  );
}

function MigrationDraftTableContent({
  plans,
  onOpen,
}: {
  plans: MigrationPlan[];
  onOpen: (planId: string) => void;
}) {
  const filters = useTableFilters(MIGRATION_DRAFTS, plans, { syncUrl: false });
  const rows = filters.filteredRows;
  const discard = useDiscardMigrationPlan();
  const [discarding, setDiscarding] = useState<MigrationPlan | null>(null);
  // Held through the close fade, so the dialog body does not blank while it leaves.
  const [shownDiscard, setShownDiscard] = useState<MigrationPlan | null>(null);
  const [discardError, setDiscardError] = useState<string | null>(null);

  const confirmDiscard = (plan: MigrationPlan) => {
    setDiscardError(null);
    discard.mutate(plan.plan_id, {
      onSuccess: () => setDiscarding(null),
      onError: (error) => setDiscardError(error instanceof Error ? error.message : "The draft could not be discarded."),
    });
  };

  return (
    <div className="space-y-4">
      <TableToolbar filters={filters} noun="migration drafts" />
      <Table scrollLabel="migration drafts">
        <TableHeader>
          <TableRow>
            <TableHead>Source</TableHead>
            <TableHead>Destination</TableHead>
            <TableHead>Operation</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Expires</TableHead>
            <TableHead pinned="end" className="w-24 text-right"><span className="sr-only">Actions</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.length === 0 ? (
            <TableEmptyRow
              colSpan={6}
              icon={FileClock}
              title={plans.length ? "No draft matches the active filters" : "No saved drafts"}
              description={plans.length
                ? "Clear or adjust the filters to see other drafts."
                : "A draft is saved after destination preflight completes."}
            />
          ) : (
            rows.map((plan) => {
              const expired = Date.parse(plan.expires_at) <= Date.now();
              return (
                <TableRow key={plan.plan_id}>
                  <TableCell className="font-medium">
                    <TableLink onClick={() => onOpen(plan.plan_id)}>{endpointPrimary(plan.source)}</TableLink>
                    <TableSubText>{endpointLabel(plan.source)}</TableSubText>
                  </TableCell>
                  <TableCell>
                    {endpointPrimary(plan.target)}
                    <TableSubText>{endpointLabel(plan.target)}</TableSubText>
                  </TableCell>
                  <TableCell>{formatLabel(plan.intent)} {formatLabel(plan.scope)}</TableCell>
                  <TableCell>
                    {draftStatusLabel(plan, expired)}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">{formatDateTime(plan.expires_at)}</TableCell>
                  <TableCell pinned="end" className="whitespace-nowrap text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-11 w-11 p-0 sm:h-8 sm:w-8"
                      aria-label={`Resume migration draft for ${endpointPrimary(plan.source)}`}
                      onClick={() => onOpen(plan.plan_id)}
                    >
                      <Edit3 className="h-4 w-4" aria-hidden />
                    </Button>
                    {/* A started plan stays with its migration; the server refuses to discard it. */}
                    {plan.status !== "executed" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-11 w-11 p-0 text-destructive hover:text-destructive sm:h-8 sm:w-8"
                        aria-label={`Discard migration draft for ${endpointPrimary(plan.source)}`}
                        onClick={() => {
                          setDiscardError(null);
                          setShownDiscard(plan);
                          setDiscarding(plan);
                        }}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>

      <DestructiveConfirmDialog
        open={discarding !== null}
        onOpenChange={(open) => !open && setDiscarding(null)}
        title="Discard Migration Draft?"
        description={shownDiscard ? (
          <>
            This deletes the draft to {shownDiscard.intent}{" "}
            <span className="break-words font-medium text-foreground" translate="no">{endpointLabel(shownDiscard.source)}</span>
            {" "}to{" "}
            <span className="break-words font-medium text-foreground" translate="no">{endpointLabel(shownDiscard.target)}</span>
            {" "}and its preflight results. Nothing on either cluster changes. It cannot be undone.
          </>
        ) : ""}
        confirmLabel="Discard Draft"
        pendingLabel="Discarding…"
        pending={discard.isPending}
        onConfirm={() => discarding && confirmDiscard(discarding)}
        error={discardError}
      />
    </div>
  );
}
