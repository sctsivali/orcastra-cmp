"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Cpu, HardDrive, Server, Database, Network, Package, Info, Monitor } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { LoadingLottiePageEnhanced } from "@/components/loading-lottie";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { NodeStatus, NodeEnvironment } from "@/types/api";

interface HardwareCategory {
  icon: React.ElementType;
  title: string;
  description: string;
  items: { label: string; value: string; detail?: string }[];
}

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

function cleanDriverVersion(value?: string | null): string {
  const normalized = cleanVersion(value);
  if (!normalized) return "";
  const parts = normalized.split("|").map((part) => part.trim()).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : normalized;
}

function formatStorageVersion(env: NodeEnvironment | null, nodes: NodeStatus[]): string {
  const storageName = cleanVersion(env?.storage as string | undefined);
  const storageVersion = cleanVersion(env?.storage_version as string | undefined);

  if (storageName && storageVersion) return `${storageName} ${storageVersion}`;
  if (storageVersion) return storageVersion;
  if (storageName) return storageName;

  const storageBackends = new Set(
    nodes.flatMap((node) => node.resources?.storage_pools?.map((pool) => pool.driver) || [])
  );

  return Array.from(storageBackends).filter(Boolean).join(", ");
}

// Helper function to generate software stack with real versions
function generateSoftwareStack(nodes: NodeStatus[]): SoftwareItem[] {
  // Aggregate environment data from connected nodes
  const connectedNodes = nodes.filter(n => n.connected && n.environment);
  const firstEnv: NodeEnvironment | null = connectedNodes[0]?.environment || null;
  
  // Get versions from first connected node's environment
  const lxdVersion = cleanVersion(connectedNodes[0]?.server_version || firstEnv?.server_version as string | undefined);
  const kernelVersion = cleanVersion(firstEnv?.kernel_version);
  const osName = String(firstEnv?.os_name || "Linux");
  const osVersion = cleanVersion(firstEnv?.os_version);
  const driverVersion = cleanDriverVersion(firstEnv?.driver_version as string | undefined);
  const storageVersion = formatStorageVersion(firstEnv, connectedNodes);
  const firewallStack = cleanVersion(firstEnv?.firewall as string | undefined);
  
  return [
    {
      name: "LXD",
      version: lxdVersion,
      purpose: "System container and virtual machine manager",
      category: "Virtualization",
      icon: Server,
    },
    {
      name: osName,
      version: osVersion,
      purpose: "Host operating system reported by the connected LXD node inventory",
      category: "Operating System",
      icon: Server,
    },
    {
      name: "Linux Kernel",
      version: kernelVersion,
      purpose: "Operating system core providing hardware abstraction",
      category: "Operating System",
      icon: Cpu,
    },
    {
      name: "Storage Backend",
      version: storageVersion,
      purpose: "Storage pool backend used for volumes, images, snapshots, and instance disks",
      category: "Storage",
      icon: Database,
    },
    {
      name: "QEMU/KVM",
      version: driverVersion,
      purpose: "Virtual machine acceleration provided through Linux KVM and the LXD VM driver",
      category: "Virtualization",
      icon: Server,
    },
    {
      name: "Network Stack",
      version: firewallStack,
      purpose: "Linux bridge, firewall, and optional OVN/Open vSwitch networking used by LXD networks",
      category: "Networking",
      icon: Network,
    },
    {
      name: "LXC",
      version: "",
      purpose: "Linux system container runtime used by LXD for container workloads",
      category: "Container Runtime",
      icon: Package,
    },
  ];
}

export default function TechnologiesPage() {
  const { data: nodesResponse, isLoading } = useQuery<{ nodes: NodeStatus[]; all_connected: boolean }>({
    queryKey: ["nodes"],
    queryFn: api.nodes.status,
  });
  
  const nodes = nodesResponse?.nodes || [];

  // Generate dynamic software stack with real versions
  const softwareStack = React.useMemo(() => generateSoftwareStack(nodes), [nodes]);

  // Aggregate hardware information from all nodes
  const hardwareInfo = React.useMemo(() => {
    if (!nodes || nodes.length === 0) return null;

    const connectedNodes = nodes.filter(n => n.connected);
    
    const totalCores = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.cpu_total || 0);
    }, 0);

    const totalMemory = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.memory_total || 0);
    }, 0);

    // Get real architecture from node environment
    const cpuArchitectures = new Set<string>(
      nodes
        .map((node: NodeStatus) => node.environment?.kernel_architecture)
        .filter((arch): arch is string => Boolean(arch))
    );

    // Get total storage capacity
    const totalStorageCapacity = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.storage_total || 0);
    }, 0);

    const totalStorageAvailable = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.storage_available || 0);
    }, 0);

    // Get GPU totals
    const totalGPUs = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.gpu_total || 0);
    }, 0);

    const storageBackends = new Set<string>(
      nodes.flatMap((node: NodeStatus) =>
        node.resources?.storage_pools
          ? node.resources.storage_pools.map((pool) => pool.driver)
          : []
      )
    );

    const categories: HardwareCategory[] = [
      {
        icon: Cpu,
        title: "CPU Resources",
        description: "Processor information across all nodes",
        items: [
          {
            label: "Total Cores",
            value: totalCores.toString(),
            detail: "Aggregate across all nodes",
          },
          {
            label: "Architectures",
            value: cpuArchitectures.size > 0 ? Array.from(cpuArchitectures).join(", ") : "x86_64",
            detail: "Supported CPU architectures",
          },
          {
            label: "Nodes",
            value: connectedNodes.length.toString(),
            detail: "Active compute nodes",
          },
        ],
      },
      {
        icon: HardDrive,
        title: "Memory",
        description: "RAM resources available in the cluster",
        items: [
          {
            label: "Total Memory",
            value: `${(totalMemory / (1024 ** 3)).toFixed(2)} GB`,
            detail: "Aggregate across all nodes",
          },
          {
            label: "Memory Type",
            value: "DDR4/DDR5",
            detail: "Modern high-performance RAM",
          },
          {
            label: "Memory per Node",
            value: connectedNodes.length > 0 ? `~${(totalMemory / connectedNodes.length / (1024 ** 3)).toFixed(2)} GB` : "N/A",
            detail: "Average memory allocation",
          },
        ],
      },
      {
        icon: Database,
        title: "Storage",
        description: "Storage systems and backends",
        items: [
          {
            label: "Storage Backends",
            value: storageBackends.size > 0 ? Array.from(storageBackends).join(", ") : "dir, zfs, btrfs",
            detail: "Available storage types",
          },
          {
            label: "Storage Pools",
            value: nodes
              .reduce(
                (sum: number, node: NodeStatus) =>
                  sum + (node.resources?.storage_pools?.length || 0),
                0
              )
              .toString(),
            detail: "Total configured storage pools",
          },
          {
            label: "Features",
            value: "Snapshots, Clones, Compression",
            detail: "Advanced storage capabilities",
          },
        ],
      },
      {
        icon: Network,
        title: "Network",
        description: "Network infrastructure and connectivity",
        items: [
          {
            label: "Network Type",
            value: "Software-Defined Networking",
            detail: "Flexible virtual network infrastructure",
          },
          {
            label: "Protocol Support",
            value: "TCP/IP, IPv4/IPv6",
            detail: "Standard network protocols",
          },
          {
            label: "Virtual Networks",
            value: "OVS, Bridge",
            detail: "Network virtualization technologies",
          },
        ],
      },
    ];

    // Add GPU category if GPUs are available
    if (totalGPUs > 0) {
      categories.push({
        icon: Monitor,
        title: "GPU Resources",
        description: "Graphics processing units for acceleration",
        items: [
          {
            label: "Total GPUs",
            value: totalGPUs.toString(),
            detail: "Aggregate across all nodes",
          },
          {
            label: "GPU Type",
            value: "NVIDIA/AMD",
            detail: "Supported GPU vendors",
          },
          {
            label: "Use Cases",
            value: "AI/ML, Rendering, HPC",
            detail: "Accelerated computing workloads",
          },
        ],
      });
    }

    return categories;
  }, [nodes]);

  if (isLoading) {
    return (
      <div className="container py-8">
        <LoadingLottiePageEnhanced context="hardware" />
      </div>
    );
  }

  return (
    <div className="container py-8 space-y-8">
      <div>
        <h1 className="text-4xl font-bold mb-2">Technologies</h1>
        <p className="text-muted-foreground text-lg">
          Explore the hardware and software powering the Orcastra platform
        </p>
      </div>

      <Tabs defaultValue="hardware" className="w-full">
        <TabsList className="grid w-full max-w-md grid-cols-2 h-auto">
          <TabsTrigger value="hardware" className="py-2.5">Hardware</TabsTrigger>
          <TabsTrigger value="software" className="py-2.5">Software</TabsTrigger>
        </TabsList>

        {/* Hardware Tab */}
        <TabsContent value="hardware" className="space-y-6 mt-6">
          <Card className="bg-muted/50">
            <CardContent className="p-6">
              <div className="flex items-start gap-3">
                <Info className="h-5 w-5 text-blue-500 mt-0.5" />
                <div>
                  <p className="text-sm font-medium mb-1">
                    Real-time Data from Production Cluster
                  </p>
                  <p className="text-sm text-muted-foreground">
                    The hardware information below is aggregated from live data collected across all nodes in the Orcastra cluster. This ensures accurate, up-to-date resource information.
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          {hardwareInfo && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {hardwareInfo.map((category) => {
                const Icon = category.icon;
                return (
                  <Card key={category.title} className="hover:shadow-md transition-all duration-200">
                    <CardHeader>
                      <div className="flex items-center gap-3">
                        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-foreground text-background">
                          <Icon className="h-5 w-5" />
                        </div>
                        <div>
                          <CardTitle>{category.title}</CardTitle>
                          <CardDescription>{category.description}</CardDescription>
                        </div>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      {category.items.map((item) => (
                        <div
                          key={item.label}
                          className="flex flex-col gap-2 pb-4 mb-4 border-b last:border-b-0 last:pb-0 last:mb-0"
                        >
                          <div className="flex justify-between items-start gap-4">
                            <span className="text-sm font-medium text-muted-foreground flex-shrink-0">
                              {item.label}
                            </span>
                            <span className="text-sm font-semibold text-right break-words">
                              {item.value}
                            </span>
                          </div>
                          {item.detail && (
                            <span className="text-xs text-muted-foreground mt-1">
                              {item.detail}
                            </span>
                          )}
                        </div>
                      ))}
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          )}

          {/* Hardware Brands & Vendors */}
          <Card>
            <CardHeader>
              <CardTitle>Available Hardware</CardTitle>
              <CardDescription>
                Compatible with industry-standard server hardware
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {[
                  { name: "Intel", logo: "/brands/intel.png" },
                  { name: "AMD", logo: "/brands/amd.png" },
                  { name: "Dell", logo: "/brands/dell.png" },
                  { name: "HP", logo: "/brands/hp.png" },
                  { name: "Supermicro", logo: "/brands/supermicro.png" },
                  { name: "Lenovo", logo: "/brands/lenovo.png" },
                  { name: "Cisco", logo: "/brands/cisco.png" },
                  { name: "Western Digital", logo: "/brands/wd.png" },
                ].map((brand) => (
                  <div
                    key={brand.name}
                    className="flex items-center justify-center p-6 rounded-lg border bg-card hover:bg-muted/50 hover:border-primary/20 transition-all duration-200 group"
                  >
                    <img
                      src={brand.logo}
                      alt={brand.name}
                      className="h-8 w-auto object-contain brightness-0 dark:invert opacity-70 group-hover:opacity-100 transition-opacity"
                      title={brand.name}
                    />
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        {/* Software Tab */}
        <TabsContent value="software" className="space-y-6 mt-6">
          <Card className="bg-muted/50">
            <CardContent className="p-6">
              <div className="flex items-start gap-3">
                <Info className="h-5 w-5 text-blue-500 mt-0.5" />
                <div>
                  <p className="text-sm font-medium mb-1">
                    Live Production Software Inventory
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Versions below are detected from connected LXD nodes. Missing version badges mean the node API does not report that component directly; updates are managed through the host operating system and LXD channel.
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {softwareStack.map((software) => {
              const Icon = software.icon;
              return (
                <Card key={software.name} className="hover:shadow-md transition-all duration-200 hover:-translate-y-0.5">
                  <CardHeader>
                    <div className="flex items-start justify-between">
                      <div className="flex items-center gap-3">
                        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-foreground text-background">
                          <Icon className="h-5 w-5" />
                        </div>
                        <div>
                          <CardTitle className="text-lg">{software.name}</CardTitle>
                          {software.version && (
                            <Badge variant="secondary" className="mt-1 text-xs font-mono">
                              {software.version}
                            </Badge>
                          )}
                        </div>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div>
                      <p className="text-xs font-medium text-muted-foreground uppercase mb-1">
                        Category
                      </p>
                      <Badge variant="outline" className="font-normal">{software.category}</Badge>
                    </div>
                    <div>
                      <p className="text-xs font-medium text-muted-foreground uppercase mb-1">
                        Purpose
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {software.purpose}
                      </p>
                    </div>
                  </CardContent>
                </Card>
              );
            })}
          </div>

          {/* Additional Software Info */}
          <Card>
            <CardHeader>
              <CardTitle>Technology Advantages</CardTitle>
              <CardDescription>
                Why we chose these technologies for Orcastra
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="flex gap-3 p-4 rounded-lg bg-muted/40 border border-border">
                  <div className="mt-0.5">
                    <div className="h-2.5 w-2.5 rounded-full bg-foreground/60" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Open Source & Community-Driven</p>
                    <p className="text-sm text-muted-foreground mt-1">
                      All core technologies are open-source, ensuring transparency, security, and active community support.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3 p-4 rounded-lg bg-muted/40 border border-border">
                  <div className="mt-0.5">
                    <div className="h-2.5 w-2.5 rounded-full bg-foreground/60" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Enterprise-Grade Reliability</p>
                    <p className="text-sm text-muted-foreground mt-1">
                      Battle-tested in production environments, these tools power some of the world's largest infrastructures.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3 p-4 rounded-lg bg-muted/40 border border-border">
                  <div className="mt-0.5">
                    <div className="h-2.5 w-2.5 rounded-full bg-foreground/60" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Modern Architecture</p>
                    <p className="text-sm text-muted-foreground mt-1">
                      Built with modern cloud-native principles for scalability, flexibility, and performance.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3 p-4 rounded-lg bg-muted/40 border border-border">
                  <div className="mt-0.5">
                    <div className="h-2.5 w-2.5 rounded-full bg-foreground/60" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Security First</p>
                    <p className="text-sm text-muted-foreground mt-1">
                      Hardware-level isolation, encrypted storage, and secure networking built into every layer.
                    </p>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
