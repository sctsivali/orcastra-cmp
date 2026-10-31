"use client";

import { Suspense } from "react";
import { ArrowRight, Eye, Route } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { TableToolbar } from "@/components/ui/table-toolbar";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatDateTimeSeconds, formatLabel } from "@/lib/utils";
import type { MigrationJob } from "@/types/migrations";
import { endpointLabel, endpointPrimary, jobProgress, migrationStatusMeta, operationLabel, strategyLabel } from "./migration-format";

const MIGRATION_JOBS = defineTable<MigrationJob>()({
  id: "migration-job",
  searchPlaceholder: "Search migration jobs",
  defaultSort: { field: "updated", direction: "desc" },
  searchExtra: (job) => [job.job_id, job.strategy, job.error_message],
  columns: [
    {
      field: "source",
      label: "Source",
      sort: (job) => endpointLabel(job.source),
      search: (job) => endpointLabel(job.source),
      filter: fromSort("text"),
    },
    {
      field: "destination",
      label: "Destination",
      sort: (job) => endpointLabel(job.target),
      search: (job) => endpointLabel(job.target),
      filter: fromSort("text"),
    },
    {
      field: "operation",
      label: "Operation",
      sort: (job) => `${job.intent} ${job.scope}`,
      facet: (job) => `${job.intent} ${job.scope}`,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "status",
      label: "Status",
      sort: (job) => job.phase,
      facet: (job) => job.phase,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "progress",
      label: "Progress",
      sort: (job) => jobProgress(job),
      filter: fromSort("number", { unit: "percent" }),
    },
    {
      field: "updated",
      label: "Updated",
      sort: (job) => Date.parse(job.updated_at),
      filter: fromSort("date", { horizon: "past" }),
    },
    {
      field: "rollback",
      label: "Retention Until",
      sort: (job) => job.retention_deadline ? Date.parse(job.retention_deadline) : null,
      filter: fromSort("date", { horizon: "future" }),
    },
  ],
});

interface MigrationJobTableProps {
  jobs: MigrationJob[];
  emptyTitle: string;
  emptyDescription: string;
  onOpen: (jobId: string) => void;
}

export function MigrationJobTable({
  jobs,
  emptyTitle,
  emptyDescription,
  onOpen,
}: MigrationJobTableProps) {
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground" role="status">Loading migration filters...</p>}>
      <MigrationJobTableContent
        jobs={jobs}
        emptyTitle={emptyTitle}
        emptyDescription={emptyDescription}
        onOpen={onOpen}
      />
    </Suspense>
  );
}

function MigrationJobTableContent({
  jobs,
  emptyTitle,
  emptyDescription,
  onOpen,
}: MigrationJobTableProps) {
  const filters = useTableFilters(MIGRATION_JOBS, jobs, { syncUrl: false });
  const rows = filters.filteredRows;

  return (
    <div className="space-y-4">
      <TableToolbar filters={filters} noun="migration jobs" />
      <Table scrollLabel="migration jobs">
        <TableHeader>
          <TableRow>
            <TableHead>Source</TableHead>
            <TableHead><span className="sr-only">Direction</span></TableHead>
            <TableHead>Destination</TableHead>
            <TableHead>Operation</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="min-w-[140px]">Progress</TableHead>
            <TableHead>Updated</TableHead>
            <TableHead>Retention Until</TableHead>
            <TableHead pinned="end" className="w-12 text-right"><span className="sr-only">Actions</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.length === 0 ? (
            <TableEmptyRow
              colSpan={9}
              icon={Route}
              title={jobs.length ? "No migration matches the active filters" : emptyTitle}
              description={jobs.length ? "Clear or adjust the filters to see other jobs." : emptyDescription}
            />
          ) : (
            rows.map((job) => {
              const status = migrationStatusMeta(job);
              const progress = jobProgress(job);
              return (
                <TableRow key={job.job_id}>
                  <TableCell className="font-medium">
                    <TableLink onClick={() => onOpen(job.job_id)}>{endpointPrimary(job.source)}</TableLink>
                    <TableSubText>{endpointLabel(job.source)}</TableSubText>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    <ArrowRight className="h-4 w-4" aria-hidden />
                  </TableCell>
                  <TableCell>
                    {endpointPrimary(job.target)}
                    <TableSubText>{endpointLabel(job.target)}</TableSubText>
                  </TableCell>
                  <TableCell>
                    {operationLabel(job)}
                    {job.strategy && <TableSubText>{strategyLabel(job)}</TableSubText>}
                  </TableCell>
                  <TableCell>
                    <Badge variant={status.tone}>{status.label}</Badge>
                    {job.error_message && <TableSubText>{job.error_message}</TableSubText>}
                  </TableCell>
                  <TableCell>
                    {progress !== null && progress !== undefined ? (
                      <div className="space-y-1">
                        <Progress value={progress} className="h-2" aria-label={`${endpointPrimary(job.source)} migration progress`} />
                        <TableSubText>{Math.round(progress)}%</TableSubText>
                      </div>
                    ) : (
                      <TableEmptyValue label="Progress not reported" />
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap">{formatDateTimeSeconds(job.updated_at)}</TableCell>
                  <TableCell className="whitespace-nowrap">
                    {job.retention_deadline ? (
                      formatDateTimeSeconds(job.retention_deadline)
                    ) : (
                      <TableEmptyValue label="No source retention deadline" />
                    )}
                  </TableCell>
                  <TableCell pinned="end" className="text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-11 w-11 p-0 sm:h-8 sm:w-8"
                      aria-label={`View migration for ${endpointPrimary(job.source)}`}
                      onClick={() => onOpen(job.job_id)}
                    >
                      <Eye className="h-4 w-4" aria-hidden />
                    </Button>
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>
    </div>
  );
}
