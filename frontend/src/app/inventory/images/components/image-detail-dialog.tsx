"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Box } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { TableLink } from "@/components/ui/table-link";
import { RecordDetailDialog } from "@/components/detail/detail-dialog";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { formatBytes, formatDateTime, formatInstanceType } from "@/lib/utils";
import type { ImageInfo, VMInfo } from "@/types/api";

// The images table had zero row interaction: no drill-down, no copy, no way to read a
// fingerprint in full even though the fingerprint is the only thing that identifies an image.
//
// The "instances using this image" section is a client-side join over the instances list the
// page already fetches, on `volatile.base_image`, which is the same key the backend uses for
// tenant filtering (routers/images.py:189). No new endpoint, and one request for the page
// rather than one per row.
//
// important: the heading says "Your instances", not "Instances". The list it joins against is
// tenant-filtered server-side, so the count genuinely differs by role and a heading implying
// totality would be wrong for everyone except an admin.
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
  if (!image) return null;

  // important: the description first. This used to title itself with a 12 character slice of the
  // fingerprint whenever an image had no alias, so the dialog opened to show what the table had
  // cut and announced itself with the same cut value. The whole fingerprint is one line below,
  // in full, with a copy button, because RecordDetailDialog requires recordId for exactly this.
  // The aliases have their own section, so they are not repeated in a subtitle.
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
            {/* important: raw. formatLabel renders x86_64 as "X86 64". */}
            <DetailField label="Architecture" value={image.architecture} mono />
            <DetailField label="Type" value={formatInstanceType(image.type)} />
            <DetailField label="Size" value={formatBytes(image.size)} />
            <DetailField label="Node" value={image.node} />
            <DetailField label="Cached" value={image.cached ? "Yes" : "No"} />
            <DetailField label="Auto-update" value={image.auto_update ? "Yes" : "No"} />
          </DetailGrid>
        </DetailSection>

        <DetailSection title="Dates">
          <DetailGrid cols={3}>
            <DetailField label="Uploaded" value={formatDateTime(image.upload_date)} />
            <DetailField label="Created" value={formatDateTime(image.created_at)} />
            <DetailField label="Last used" value={formatDateTime(image.last_used_at)} />
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
              {instances.map((instance) => (
                <li key={`${instance.host}-${instance.name}`} className="flex items-center justify-between px-3 py-2">
                  <TableLink
                    href={`/inventory/instances?q=${encodeURIComponent(instance.name)}&project=${encodeURIComponent(instance.project || "default")}&host=${encodeURIComponent(instance.host)}`}
                    title={`Open ${instance.name} in Instances`}
                  >
                    {instance.name}
                  </TableLink>
                  <span className="text-xs text-muted-foreground">{instance.host}</span>
                </li>
              ))}
            </ul>
          )}
        </DetailSection>
      </div>
    </RecordDetailDialog>
  );
}
