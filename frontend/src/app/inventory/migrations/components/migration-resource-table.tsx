"use client";

import { Suspense } from "react";
import { Boxes } from "lucide-react";
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
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatBytes, formatLabel } from "@/lib/utils";
import type { MigrationJobResource, MigrationPlannedResource } from "@/types/migrations";
import { resourceLocation, resourceName, resourceProgress } from "./migration-format";

function isJobResource(resource: MigrationPlannedResource): resource is MigrationJobResource {
  return "resource_id" in resource;
}

const MIGRATION_RESOURCES = defineTable<MigrationPlannedResource>()({
  id: "migration-resource",
  searchPlaceholder: "Search migration resources",
  defaultSort: { field: "resource", direction: "asc" },
  searchExtra: (resource) => [resource.resource_key, resourceLocation(resource.source)],
  columns: [
    {
      field: "resource",
      label: "Resource",
      sort: resourceName,
      search: resourceName,
      filter: fromSort("text"),
    },
    {
      field: "type",
      label: "Type",
      sort: (resource) => resource.resource_type,
      facet: (resource) => resource.resource_type,
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "target",
      label: "Target",
      sort: (resource) => resourceLocation(resource.target),
      search: (resource) => resourceLocation(resource.target),
      filter: fromSort("text", { emptyLabel: "Not mapped" }),
    },
    {
      field: "status",
      label: "Status",
      sort: (resource) => isJobResource(resource) ? resource.state : "planned",
      facet: (resource) => isJobResource(resource) ? resource.state : "planned",
      facetSpec: { primary: true, format: formatLabel },
    },
    {
      field: "progress",
      label: "Progress",
      sort: (resource) => isJobResource(resource) ? resourceProgress(resource)?.percent : null,
      filter: fromSort("number", { unit: "percent" }),
    },
  ],
});

export function MigrationResourceTable({ resources }: { resources: MigrationPlannedResource[] }) {
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground" role="status">Loading resource filters...</p>}>
      <MigrationResourceTableContent resources={resources} />
    </Suspense>
  );
}

function MigrationResourceTableContent({ resources }: { resources: MigrationPlannedResource[] }) {
  const filters = useTableFilters(MIGRATION_RESOURCES, resources, { syncUrl: false });
  const rows = filters.filteredRows;

  return (
    <div className="space-y-4">
      <TableToolbar filters={filters} noun="migration resources" />
      <Table containerClassName="rounded-md border" scrollLabel="migration resources">
        <TableHeader>
          <TableRow>
            <TableHead>Resource</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Target</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="min-w-[160px]">Progress</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.length === 0 ? (
            <TableEmptyRow
              colSpan={5}
              icon={Boxes}
              title={resources.length ? "No resource matches the active filters" : "No resources reported"}
              description={resources.length
                ? "Clear or adjust the filters to see other resources."
                : "Resource dependencies will appear after the server completes discovery."}
            />
          ) : (
            rows.map((resource) => {
              const jobResource = isJobResource(resource) ? resource : null;
              const progress = jobResource ? resourceProgress(jobResource) : null;
              const name = resourceName(resource);
              return (
                <TableRow key={resource.resource_key}>
                  <TableCell className="font-medium">
                    {name}
                    {resourceLocation(resource.source) && (
                      <TableSubText>{resourceLocation(resource.source)}</TableSubText>
                    )}
                  </TableCell>
                  <TableCell>{formatLabel(resource.resource_type)}</TableCell>
                  <TableCell>
                    {resourceLocation(resource.target) || <TableEmptyValue label="Not mapped" />}
                  </TableCell>
                  <TableCell>
                    {jobResource ? formatLabel(jobResource.state) : "Planned"}
                    {jobResource?.error_message && <TableSubText>{jobResource.error_message}</TableSubText>}
                  </TableCell>
                  <TableCell>
                    {progress?.percent !== null && progress?.percent !== undefined ? (
                      <div className="space-y-1">
                        <Progress value={progress.percent} className="h-2" aria-label={`${name} progress`} />
                        <TableSubText>
                          {Math.round(progress.percent)}%
                          {progress.transferred !== null && progress.total
                            ? `, ${formatBytes(progress.transferred)} of ${formatBytes(progress.total)}`
                            : ""}
                        </TableSubText>
                      </div>
                    ) : (
                      <TableEmptyValue label="Progress not reported" />
                    )}
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
