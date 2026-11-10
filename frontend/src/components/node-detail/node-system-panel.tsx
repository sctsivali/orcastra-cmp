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
 * The System tab. It reads only the list row and never NodeResources, so the modal leaves the
 * live per-cluster poll off while this tab is open.
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
          {/* Auth mode and the storage driver are operator detail about the hypervisor, so a
              tenant does not see them. */}
          {isPartner ? <Field label="Authentication" value={node.auth || EMPTY} /> : null}
          <Field label="Server Name" value={String(env?.server_name || EMPTY)} />
          <Field label="OS" value={osVersion ? `${osName} ${osVersion}` : osName} />
          <Field label="Kernel" value={String(env?.kernel_version || EMPTY)} />
          {/* Never through formatLabel. It title-cases raw enums and turns x86_64 into "X86 64". */}
          <Field label="Architecture" value={String(env?.kernel_architecture || EMPTY)} />
          <Field label="Clustering" value={env?.server_clustered ? "Clustered" : "Standalone server"} />
          {isPartner ? <Field label="Storage Backend" value={String(env?.storage || EMPTY)} /> : null}
          <Field label="Firewall" value={String(env?.firewall || EMPTY)} />
        </div>
        {/* The same verdict as the Overview card, beside the version it is about. */}
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
        {/* Every member's verdict, patched included, since this is where kernels are read. */}
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
