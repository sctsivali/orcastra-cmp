"use client";

import * as React from "react";
import { Cpu, Server, Database, Network, Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatHypervisorType, formatLabel } from "@/lib/utils";
import type { NodeStatus, NodeEnvironment } from "@/types/api";

interface SoftwareItem {
  name: string;
  version: string;
  purpose: string;
  category: string;
  icon: React.ElementType;
}

function cleanVersion(value?: string | null): string {
  return String(value || "").replace(/^v/i, "").trim();
}

// driver_version can be a pipe-joined chain (ex; "8.2.0 | 4.0.0"); the hypervisor VM driver
// (QEMU) is the last segment.
function cleanDriverVersion(value?: string | null): string {
  const normalized = cleanVersion(value);
  if (!normalized) return "";
  const parts = normalized.split("|").map((part) => part.trim()).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : normalized;
}

function formatStorageVersion(env: NodeEnvironment | null, nodes: NodeStatus[]): string {
  const storageName = cleanVersion(env?.storage as string | undefined);
  const storageVersion = cleanVersion(env?.storage_version as string | undefined);

  if (storageName && storageVersion) return `${formatLabel(storageName)} ${storageVersion}`;
  if (storageVersion) return storageVersion;
  if (storageName) return formatLabel(storageName);

  const storageBackends = new Set(
    nodes.flatMap((node) => node.resources?.storage_pools?.map((pool) => pool.driver) || [])
  );
  return Array.from(storageBackends).filter(Boolean).map(formatLabel).join(", ");
}

function formatDetectedHypervisorName(nodes: NodeStatus[]): string {
  const detectedTypes = Array.from(
    new Set(
      nodes
        .map((node) => node.node_type)
        .filter((type): type is string => Boolean(type?.trim()))
        .map(formatHypervisorType)
    )
  );
  if (detectedTypes.length === 1) return detectedTypes[0];
  if (detectedTypes.length > 1) return "Mixed Hypervisors";
  return "Hypervisor Platform";
}

// Build the software inventory from the live node environment. Every value is read from the
// connected node; items with no real version simply omit the badge. No fabricated entries.
function buildSoftwareStack(nodes: NodeStatus[]): SoftwareItem[] {
  const connectedNodes = nodes.filter((n) => n.connected && n.environment);
  const env: NodeEnvironment | null = connectedNodes[0]?.environment || null;

  const items: SoftwareItem[] = [
    {
      name: formatDetectedHypervisorName(connectedNodes),
      version: cleanVersion(connectedNodes[0]?.server_version || (env?.server_version as string | undefined)),
      purpose: "System container and virtual machine manager driving the platform",
      category: "Virtualization",
      icon: Server,
    },
    {
      name: String(env?.os_name || "Linux"),
      version: cleanVersion(env?.os_version),
      purpose: "Host operating system reported by the connected node inventory",
      category: "Operating System",
      icon: Server,
    },
    {
      name: "Linux Kernel",
      version: cleanVersion(env?.kernel_version),
      purpose: "Operating system core providing hardware abstraction and isolation primitives",
      category: "Operating System",
      icon: Cpu,
    },
    {
      name: "QEMU/KVM",
      version: cleanDriverVersion(env?.driver_version as string | undefined),
      purpose: "Virtual machine acceleration through Linux KVM and the hypervisor VM driver",
      category: "Virtualization",
      icon: Server,
    },
    {
      name: "Storage Backend",
      version: formatStorageVersion(env, connectedNodes),
      purpose: "Pool backend for volumes, images, snapshots, and instance disks",
      category: "Storage",
      icon: Database,
    },
  ];

  // Firewall is only meaningful when the node reports it - keeps the card honest.
  const firewall = cleanVersion(env?.firewall as string | undefined);
  if (firewall) {
    items.push({
      name: "Network Firewall",
      version: formatLabel(firewall),
      purpose: "Packet filtering driver backing hypervisor bridges and instance networking",
      category: "Networking",
      icon: Network,
    });
  }

  return items;
}

function PlatformDetails({ nodes }: { nodes: NodeStatus[] }) {
  const node = nodes.find((n) => n.connected && n.environment);
  if (!node) return null;
  const env = node.environment as NodeEnvironment;

  const rows: { label: string; value: string }[] = [];
  if (node.api_version) rows.push({ label: "Hypervisor API Version", value: node.api_version });
  if (node.auth) rows.push({ label: "Hypervisor Authentication", value: formatLabel(node.auth) });
  if (typeof env.server_clustered === "boolean") {
    rows.push({ label: "Clustered", value: env.server_clustered ? "Yes" : "No" });
  }
  if (env.project) rows.push({ label: "Default Project", value: String(env.project) });

  const architectures = Array.from(
    new Set(
      nodes
        .flatMap((n) => [
          n.environment?.kernel_architecture,
          ...((n.environment?.architectures as string[] | undefined) || []),
        ])
        .filter((a): a is string => Boolean(a))
    )
  );

  if (rows.length === 0 && architectures.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Platform Details</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {rows.map((row) => (
          <div key={row.label} className="flex justify-between gap-4 text-sm">
            <span className="text-muted-foreground">{row.label}</span>
            <span className="font-medium text-right break-words">{row.value}</span>
          </div>
        ))}
        {architectures.length > 0 && (
          <div className="flex justify-between gap-4 text-sm items-start">
            <span className="text-muted-foreground">Architectures</span>
            <div className="flex flex-wrap justify-end gap-1.5">
              {architectures.map((arch) => (
                <Badge key={arch} variant="outline" className="font-mono text-xs">
                  {arch}
                </Badge>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function SoftwareInventory({ nodes }: { nodes: NodeStatus[] }) {
  const software = React.useMemo(() => buildSoftwareStack(nodes), [nodes]);

  return (
    <div className="space-y-6">
      <Card className="bg-muted/50">
        <CardContent className="p-6">
          <div className="flex items-start gap-3">
            <Info className="h-5 w-5 text-blue-500 mt-0.5 shrink-0" />
            <p className="text-sm text-muted-foreground">
              Versions are detected live from connected nodes. A missing version means the node API
              does not report that component directly; it is managed through the host operating
              system and platform channel.
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {software.map((item) => {
          const Icon = item.icon;
          return (
            <Card
              key={item.name}
              className="hover:shadow-md transition-all duration-200 hover:-translate-y-0.5"
            >
              <CardHeader>
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-foreground text-background">
                    <Icon className="h-5 w-5" />
                  </div>
                  <div className="min-w-0">
                    <CardTitle className="text-lg truncate">{item.name}</CardTitle>
                    {item.version && (
                      <p className="mt-1 text-xs font-mono text-muted-foreground break-all">
                        {item.version}
                      </p>
                    )}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div>
                  <p className="text-xs font-medium text-muted-foreground uppercase mb-1">Category</p>
                  <p className="text-sm">{item.category}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-muted-foreground uppercase mb-1">Purpose</p>
                  <p className="text-sm text-muted-foreground">{item.purpose}</p>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <PlatformDetails nodes={nodes} />
    </div>
  );
}
