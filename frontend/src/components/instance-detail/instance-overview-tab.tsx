"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { TableLink } from "@/components/ui/table-link";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { TagBadge } from "@/components/tag-manager";
import { formatDateTime, formatInstanceType } from "@/lib/utils";
import { instanceStatusDisplay } from "@/lib/instance-status";
import type { VMInfo } from "@/types/api";
import { inventoryLinks } from "@/lib/inventory-links";
import { NodeLink } from "@/components/ui/node-link";

// Everything the row already knew and the old modal never showed: description, architecture,
// project, timestamps, profiles, tags, the cluster member, and the image the instance came from.
export function InstanceOverviewTab({ instance }: { instance: VMInfo }) {
  const baseImage = instance.config?.["volatile.base_image"];
  const statusDisplay = instanceStatusDisplay(instance.status);

  return (
    <div className="space-y-4">
      <DetailSection title="Identity">
        <DetailGrid cols={3}>
          <DetailField label="Type" value={formatInstanceType(instance.type)} />
          {/* important: raw. formatLabel would render x86_64 as "X86 64". */}
          <DetailField label="Architecture" value={instance.architecture} mono />
          <DetailField
            label="Status"
            value={<Badge variant={statusDisplay.tone}>{statusDisplay.label}</Badge>}
          />
          <DetailField label="Project" value={instance.project || "default"} />
          <DetailField label="Cluster" value={<NodeLink clusterId={instance.host} />} />
          <DetailField label="Cluster Member" value={instance.location} />
          <DetailField label="Description" value={instance.description} span="full" />
        </DetailGrid>
      </DetailSection>

      <DetailSection title="Lifecycle">
        <DetailGrid cols={3}>
          <DetailField label="Created" value={formatDateTime(instance.created_at)} />
          <DetailField label="Last Used" value={formatDateTime(instance.last_used_at)} />
          {/* important: the whole fingerprint. This rendered 12 of its 64 characters with no
              ellipsis at all, so nothing told the operator the value had been cut, and the title
              named the action rather than the value. */}
          <DetailField
            label="Base Image"
            mono
            value={
              baseImage ? (
                <TableLink
                  wrap
                  href={inventoryLinks.images({ q: baseImage, node: instance.host })}
                  title="Open this image in Images"
                >
                  {baseImage}
                </TableLink>
              ) : undefined
            }
          />
        </DetailGrid>
      </DetailSection>

      <DetailSection title="Applied">
        <DetailGrid cols={2}>
          <DetailField
            label="Profiles"
            value={
              instance.profiles?.length ? (
                <span className="flex flex-wrap gap-1.5">
                  {instance.profiles.map((profile) => (
                    <TableLink
                      key={profile}
                      href={inventoryLinks.profiles({ q: profile, node: instance.host })}
                      title={`Open ${profile} in Profiles`}
                    >
                      {profile}
                    </TableLink>
                  ))}
                </span>
              ) : undefined
            }
          />
          <DetailField
            label="Tags"
            value={
              instance.tags?.length ? (
                <span className="flex flex-wrap gap-1.5">
                  {instance.tags.map((tag) => (
                    <TagBadge key={tag} tag={tag} size="sm" />
                  ))}
                </span>
              ) : undefined
            }
          />
        </DetailGrid>
      </DetailSection>
    </div>
  );
}
