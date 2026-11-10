"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Box } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { TableLink } from "@/components/ui/table-link";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { formatBytes, formatDateTime, formatInstanceType } from "@/lib/utils";
import type { ImageInfo, VMInfo } from "@/types/api";
import { inventoryLinks } from "@/lib/inventory-links";
import { NodeLink } from "@/components/ui/node-link";

// The "instances using this image" section is a client-side join over the instances list the
// page already fetches, on `volatile.base_image`, the same key the backend uses for tenant
// filtering (routers/images.py, _allowed_image_fingerprints_by_host). One request for the page
// instead of one per row.
//
// The heading says "Your instances". The list it joins against is tenant-filtered server-side,
// so the count differs by role and a heading implying totality would be wrong for everyone
// except an admin.
/** A dialog section, not a table: ten rows is already a long read inside a modal. */
const INSTANCE_PAGE_SIZE = PANEL_PAGE_SIZE;

export function ImageDetailDialog({
  image,
  instances,
  open,
  onOpenChange,
}: {
  image: (ImageInfo & { node?: string }) | null;
  instances: readonly VMInfo[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  // important: hooks before the early return, or opening the dialog for an image with no row
  // would change how many hooks run.
  const [page, setPage] = React.useState(1);

  // The join key is volatile.base_image, so one standard image can be the parent of nearly every
  // instance an organization runs. The section is paged so that image does not list the whole
  // visible fleet at once.
  const totalPages = Math.max(1, Math.ceil(instances.length / INSTANCE_PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const pagedInstances = instances.slice(
    (currentPage - 1) * INSTANCE_PAGE_SIZE,
    currentPage * INSTANCE_PAGE_SIZE,
  );

  // Reset when the dialog is pointed at a different image. It stays mounted between
  // openings, so without this the next image opens on the previous one's page number.
  const fingerprint = image?.fingerprint ?? null;
  const [lastFingerprint, setLastFingerprint] = React.useState(fingerprint);
  if (lastFingerprint !== fingerprint) {
    setLastFingerprint(fingerprint);
    setPage(1);
  }

  if (!image) return null;

  // The description first, then an alias, then the whole fingerprint, never a slice of it. The
  // fingerprint is also shown in full with a copy button as recordId, and the aliases have their
  // own section, so they are not repeated in a subtitle.
  const title = image.description || image.aliases?.find(Boolean) || image.fingerprint;

  return (
    <RecordDetailDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      recordId={image.fingerprint}
      recordIdLabel="Fingerprint"
      status={image.public ? <Badge variant="outline">Public</Badge> : undefined}
      size="lg"
    >
      <div className="space-y-4">
        <DetailSection title="Image">
          <DetailGrid cols={3}>
            {/* Raw: formatLabel renders x86_64 as "X86 64". */}
            <DetailField label="Architecture" value={image.architecture} mono />
            <DetailField label="Type" value={formatInstanceType(image.type)} />
            <DetailField label="Size" value={formatBytes(image.size)} />
            <DetailField label="Cluster" value={<NodeLink clusterId={image.node} />} />
            <DetailField label="Project" value={image.project} />
            <DetailField label="Cached" value={image.cached ? "Yes" : "No"} />
            <DetailField label="Auto-update" value={image.auto_update ? "Yes" : "No"} />
          </DetailGrid>
        </DetailSection>

        <DetailSection title="Dates">
          <DetailGrid cols={3}>
            <DetailField label="Uploaded" value={formatDateTime(image.upload_date)} />
            <DetailField label="Created" value={formatDateTime(image.created_at)} />
            <DetailField label="Last Used" value={formatDateTime(image.last_used_at)} />
            <DetailField label="Expires" value={formatDateTime(image.expires_at)} />
          </DetailGrid>
        </DetailSection>

        {image.aliases?.filter(Boolean).length ? (
          <DetailSection title={`Aliases (${image.aliases.filter(Boolean).length})`}>
            <div className="flex flex-wrap gap-1.5">
              {image.aliases.filter(Boolean).map((alias) => (
                <Badge key={alias} variant="outline">
                  {alias}
                </Badge>
              ))}
            </div>
          </DetailSection>
        ) : null}

        <DetailSection
          title={`Your instances using this image (${instances.length})`}
          description="Joined on volatile.base_image against the instances you can see, so another organization's instances are not counted."
        >
          {instances.length === 0 ? (
            <EmptyState icon={Box} title="No instances of yours use this image" />
          ) : (
            <ul className="divide-y rounded-md border">
              {pagedInstances.map((instance) => (
                <li key={`${instance.host}-${instance.name}`} className="flex items-center justify-between px-3 py-2">
                  <TableLink
                    href={inventoryLinks.instances({ q: instance.name, project: instance.project || "default", host: instance.host })}
                    title={`Open ${instance.name} in Instances`}
                  >
                    {instance.name}
                  </TableLink>
                  <NodeLink clusterId={instance.host} className="text-xs text-muted-foreground" />
                </li>
              ))}
            </ul>
          )}
          {/* Absent rather than disabled when everything fits. The compact variant, because this
              is a section inside a dialog rather than a table, and its label names the section so
              a screen reader can tell it apart from any other pager on screen. */}
          {totalPages > 1 && (
            <div className="mt-3">
              <DataTablePagination
                variant="compact"
                label={`instances using ${image.aliases?.[0] || "this image"}`}
                currentPage={currentPage}
                totalPages={totalPages}
                onPageChange={setPage}
              />
            </div>
          )}
        </DetailSection>
      </div>
    </RecordDetailDialog>
  );
}
