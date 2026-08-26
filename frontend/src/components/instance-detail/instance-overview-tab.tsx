"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { TableLink } from "@/components/ui/table-link";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { TagBadge } from "@/components/tag-manager";
import { formatDateTime, formatInstanceType } from "@/lib/utils";
import type { VMInfo } from "@/types/api";

// Everything the row already knew and the old modal never showed: description, architecture,
// project, timestamps, profiles, tags, the cluster member, and the image the instance came from.
export function InstanceOverviewTab({ instance }: { instance: VMInfo }) {
  const baseImage = instance.config?.["volatile.base_image"];

  return (
    <div className="space-y-4">
      <DetailSection title="Identity">
        <DetailGrid cols={3}>
          <DetailField label="Type" value={formatInstanceType(instance.type)} />
          {/* important: raw. formatLabel would render x86_64 as "X86 64". */}
          <DetailField label="Architecture" value={instance.architecture} mono />
          <DetailField label="Status" value={<Badge variant="outline">{instance.status}</Badge>} />
          <DetailField label="Project" value={instance.project || "default"} />
          <DetailField label="Node" value={instance.host} />
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
                  href={`/inventory/images?q=${encodeURIComponent(baseImage)}`}
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
                      href={`/inventory/profiles?q=${encodeURIComponent(profile)}`}
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

      {instance.resources ? (
        <DetailSection
          title="Resources"
          description="Reported by the cluster for this instance, not by the metrics poll."
        >
          <DetailGrid cols={3}>
            <DetailField label="CPU Usage" value={instance.resources.cpu_usage} />
            <DetailField label="Memory Usage" value={instance.resources.memory_usage} />
            <DetailField label="Memory Limit" value={instance.resources.memory_limit} />
          </DetailGrid>
        </DetailSection>
      ) : null}
    </div>
  );
}
