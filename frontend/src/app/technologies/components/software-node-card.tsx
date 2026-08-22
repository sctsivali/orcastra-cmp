"use client";

import * as React from "react";
import { Server } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { KeyValueList } from "@/components/used-by/panels/config-panel";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { cn, formatHypervisorType } from "@/lib/utils";
import type { NodeStatus } from "@/types/api";
import {
  countEnabled,
  detectedRuntimes,
  detectedStorage,
  flagsAsEntries,
  supportedDriverLabels,
} from "./software-fields";

/**
 * One connected node's software, entirely from its own environment block.
 *
 * Nothing here is re-cased. Driver names, storage backend names and architectures are all
 * registry-authored, and formatLabel turns x86_64 into "X86 64".
 */
export function SoftwareNodeCard({ node }: { node: NodeStatus }) {
  const env = node.environment;
  const [flagsOpen, setFlagsOpen] = React.useState(false);

  const runtimes = detectedRuntimes(env);
  const storage = detectedStorage(env);
  const supported = supportedDriverLabels(env);
  const kernelFlags = countEnabled(env?.kernel_features);
  const lxcFlags = countEnabled(env?.lxc_features);
  const hasFlags = kernelFlags.total > 0 || lxcFlags.total > 0;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <Server className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <CardTitle className="text-lg">{node.display_name || node.name}</CardTitle>
          {node.node_type ? (
            <Badge variant="outline" className="font-normal">
              {formatHypervisorType(node.node_type)}
            </Badge>
          ) : null}
          {env?.server_lts ? <Badge variant="secondary">LTS</Badge> : null}
          {env?.server_clustered ? <Badge variant="outline">Clustered</Badge> : null}
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        <DetailSection title="Platform">
          <DetailGrid cols={3}>
            <DetailField label="Hypervisor" value={node.server_version || undefined} mono />
            <DetailField label="API version" value={node.api_version || undefined} mono />
            <DetailField label="Project" value={env?.project || undefined} />
            <DetailField label="Server name" value={env?.server_name || undefined} />
            <DetailField label="Event mode" value={env?.server_event_mode || undefined} />
            <DetailField
              label="Instance types"
              value={
                Array.isArray(env?.instance_types) && env.instance_types.length > 0
                  ? env.instance_types.join(", ")
                  : undefined
              }
            />
          </DetailGrid>
        </DetailSection>

        <DetailSection title="Operating system">
          <DetailGrid cols={3}>
            <DetailField
              label="Distribution"
              value={
                env?.os_name ? `${env.os_name}${env.os_version ? ` ${env.os_version}` : ""}` : undefined
              }
            />
            <DetailField label="Kernel" value={env?.kernel_version || undefined} mono />
            <DetailField label="Architecture" value={env?.kernel_architecture || undefined} mono />
            <DetailField
              label="Supported architectures"
              value={
                Array.isArray(env?.architectures) && env.architectures.length > 0
                  ? env.architectures.join(", ")
                  : undefined
              }
              mono
              span={3}
            />
          </DetailGrid>
        </DetailSection>

        {runtimes.length > 0 ? (
          <DetailSection
            title="Instance runtimes"
            description="Each runtime with the version this node reports for it."
          >
            <DetailGrid cols={3}>
              {runtimes.map((runtime) => (
                <DetailField
                  key={runtime.name}
                  label={runtime.name}
                  value={runtime.version || undefined}
                  mono
                />
              ))}
            </DetailGrid>
          </DetailSection>
        ) : null}

        <DetailSection title="Storage and networking">
          <DetailGrid cols={3}>
            {storage.map((backend) => (
              <DetailField
                key={backend.name}
                label={`${backend.name} backend`}
                value={backend.version || undefined}
                mono
              />
            ))}
            <DetailField label="Firewall" value={env?.firewall || undefined} />
          </DetailGrid>
          {supported.length > 0 ? (
            <div className="text-sm">
              <p className="text-xs text-muted-foreground mb-1">Storage drivers available</p>
              <OverflowCell<string>
                items={supported}
                preview={3}
                noun="storage drivers"
                ownerLabel={node.display_name || node.name}
                emptyLabel="None reported"
                itemLabel="storage driver"
                renderInline={(driver) => <span className="font-mono">{driver}</span>}
                renderItem={(driver) => <span className="font-mono">{driver}</span>}
              />
            </div>
          ) : null}
        </DetailSection>

        {hasFlags ? (
          <Collapsible open={flagsOpen} onOpenChange={setFlagsOpen}>
            <CollapsibleTrigger
              aria-label={`Show kernel and container feature flags for ${node.display_name || node.name}`}
              className={cn(
                "w-full rounded-md border px-3 py-2 text-left text-sm",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                "hover:bg-muted/50",
              )}
            >
              <span className="text-muted-foreground">
                Feature flags: {kernelFlags.on} of {kernelFlags.total} kernel,{" "}
                {lxcFlags.on} of {lxcFlags.total} container
              </span>
            </CollapsibleTrigger>
            <CollapsibleContent className="pt-3 space-y-3">
              {kernelFlags.total > 0 ? (
                <div>
                  <p className="text-xs text-muted-foreground mb-1">Kernel</p>
                  <KeyValueList entries={flagsAsEntries(env?.kernel_features)} />
                </div>
              ) : null}
              {lxcFlags.total > 0 ? (
                <div>
                  <p className="text-xs text-muted-foreground mb-1">Container runtime</p>
                  <KeyValueList entries={flagsAsEntries(env?.lxc_features)} />
                </div>
              ) : null}
            </CollapsibleContent>
          </Collapsible>
        ) : null}
      </CardContent>
    </Card>
  );
}
