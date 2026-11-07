"use client";

import { EMPTY_VALUE } from "@/lib/utils";
import { Info } from "lucide-react";
import { usePermissions } from "@/hooks/use-permissions";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { KernelAdvisoryBadge } from "@/components/kernel-advisory/kernel-advisory-badge";
import { HypervisorReleaseStatus, versionRange } from "@/components/hypervisor-release";
import type { NodeStatus } from "@/types";

const EMPTY = EMPTY_VALUE;

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  );
}

/**
 * The System tab.
 *
 * note: this is the one panel that reads only the list row and never NodeResources. That is
 * the split axis for this directory, and it is what lets the modal leave the live per-cluster
 * poll switched off while an operator is reading kernel versions.
 */
export function NodeSystemPanel({ node }: { node: NodeStatus }) {
  const { isPartner } = usePermissions();
  const env = node.environment;
  const osName = String(env?.os_name || EMPTY);
  const osVersion = String(env?.os_version || "");

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Info className="h-5 w-5" />
          System Information
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Hypervisor Version"
            value={node.server_version ? versionRange(node.hypervisor_release, node.server_version) : EMPTY}
          />
          <Field label="API Version" value={node.api_version || EMPTY} />
          {/* auth mode and the storage driver are the operator's picture of the hypervisor, not
              the tenant's picture of where their instance runs */}
          {isPartner ? <Field label="Authentication" value={node.auth || EMPTY} /> : null}
          <Field label="Server Name" value={String(env?.server_name || EMPTY)} />
          <Field label="OS" value={osVersion ? `${osName} ${osVersion}` : osName} />
          <Field label="Kernel" value={String(env?.kernel_version || EMPTY)} />
          {/* note: never through formatLabel. It title-cases raw enums and turns x86_64 into "X86 64". */}
          <Field label="Architecture" value={String(env?.kernel_architecture || EMPTY)} />
          <Field label="Clustering" value={env?.server_clustered ? "Clustered" : "Standalone server"} />
          {isPartner ? <Field label="Storage Backend" value={String(env?.storage || EMPTY)} /> : null}
          <Field label="Firewall" value={String(env?.firewall || EMPTY)} />
        </div>
        {/* the same verdict as the Overview card, beside the version it is about */}
        {isPartner && node.server_version && node.hypervisor_release ? (
          <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <p className="font-medium">Version Check</p>
            <HypervisorReleaseStatus
              release={node.hypervisor_release}
              currentVersion={node.server_version}
              clustered={node.clustered}
            />
          </div>
        ) : null}
        {/* every member's verdict, patched included: this panel is where an operator reads kernels */}
        {isPartner ? (
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-muted-foreground">Kernel CVEs</p>
            <KernelAdvisoryBadge
              clusterId={node.name}
              showPatched
              fallback={<span className="text-sm text-muted-foreground">Not read yet</span>}
              unreadFallback={<span className="text-sm text-muted-foreground">Could not be read</span>}
            />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
