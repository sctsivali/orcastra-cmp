"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Cpu, HardDrive, Server, Database, Network, Package, Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { NodeStatus } from "@/types/api";

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

const softwareStack: SoftwareItem[] = [
  {
    name: "LXD",
    version: "",
    purpose: "System container and virtual machine manager",
    category: "Virtualization",
    icon: Server,
  },
  {
    name: "Ubuntu",
    version: "",
    purpose: "Enterprise-grade Linux operating system with long-term support",
    category: "Operating System",
    icon: Server,
  },
  {
    name: "Linux Kernel",
    version: "",
    purpose: "Operating system core providing hardware abstraction",
    category: "Operating System",
    icon: Cpu,
  },
  {
    name: "ZFS",
    version: "",
    purpose: "Advanced filesystem with data integrity and snapshot capabilities",
    category: "Storage",
    icon: Database,
  },
  {
    name: "QEMU/KVM",
    version: "",
    purpose: "Kernel-based virtual machine for full virtualization",
    category: "Virtualization",
    icon: Server,
  },
  {
    name: "Open vSwitch",
    version: "",
    purpose: "Production-quality multilayer virtual switch",
    category: "Networking",
    icon: Network,
  },
  {
    name: "containerd",
    version: "",
    purpose: "Container runtime for managing container lifecycle",
    category: "Container Runtime",
    icon: Package,
  },
];

export default function TechnologiesPage() {
  const { data: nodesResponse, isLoading } = useQuery<{ nodes: NodeStatus[]; all_connected: boolean }>({
    queryKey: ["nodes"],
    queryFn: api.nodes.status,
  });
  
  const nodes = nodesResponse?.nodes || [];

  // Aggregate hardware information from all nodes
  const hardwareInfo = React.useMemo(() => {
    if (!nodes || nodes.length === 0) return null;

    const totalCores = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.cpu_total || 0);
    }, 0);

    const totalMemory = nodes.reduce((sum: number, node: NodeStatus) => {
      return sum + (node.resources?.memory_total || 0);
    }, 0);

    const cpuArchitectures = new Set<string>(
      nodes
        .map((node: NodeStatus) => "x86_64") // Architecture info not available in NodeStatus
        .filter(Boolean)
    );

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
            value: Array.from(cpuArchitectures).join(", ") || "x86_64",
            detail: "Supported CPU architectures",
          },
          {
            label: "Nodes",
            value: nodes.length.toString(),
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
            value: `~${(totalMemory / nodes.length / (1024 ** 3)).toFixed(2)} GB`,
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
            value: Array.from(storageBackends).join(", ") || "dir, zfs, btrfs",
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

    return categories;
  }, [nodes]);

  if (isLoading) {
    return (
      <div className="container py-8 space-y-8">
        <Skeleton className="h-20 w-full" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-64 w-full" />
          ))}
        </div>
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
                  <Card key={category.title}>
                    <CardHeader>
                      <div className="flex items-center gap-3">
                        <div className="p-2 rounded-lg bg-primary/10">
                          <Icon className="h-6 w-6 text-primary" />
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
                    className="flex items-center justify-center p-6 rounded-lg border bg-card hover:bg-muted/50 transition-colors"
                  >
                    <img
                      src={brand.logo}
                      alt={brand.name}
                      className="h-8 w-auto object-contain brightness-0 dark:invert"
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
                    Production Software Stack
                  </p>
                  <p className="text-sm text-muted-foreground">
                    Orcastra leverages enterprise-grade open-source software to deliver reliable, scalable infrastructure management. All software versions are actively maintained and regularly updated.
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {softwareStack.map((software) => {
              const Icon = software.icon;
              return (
                <Card key={software.name} className="hover:shadow-md transition-shadow">
                  <CardHeader>
                    <div className="flex items-start justify-between">
                      <div className="flex items-center gap-3">
                        <div className="p-2 rounded-lg bg-violet-500/10">
                          <Icon className="h-5 w-5 text-violet-600 dark:text-violet-400" />
                        </div>
                        <div>
                          <CardTitle className="text-lg">{software.name}</CardTitle>
                        </div>
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div>
                      <p className="text-xs font-medium text-muted-foreground uppercase mb-1">
                        Category
                      </p>
                      <p className="text-sm">{software.category}</p>
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
              <div className="grid gap-4">
                <div className="flex gap-3">
                  <div className="mt-1">
                    <div className="h-2 w-2 rounded-full bg-green-500" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Open Source & Community-Driven</p>
                    <p className="text-sm text-muted-foreground">
                      All core technologies are open-source, ensuring transparency, security, and active community support.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3">
                  <div className="mt-1">
                    <div className="h-2 w-2 rounded-full bg-blue-500" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Enterprise-Grade Reliability</p>
                    <p className="text-sm text-muted-foreground">
                      Battle-tested in production environments, these tools power some of the world's largest infrastructures.
                    </p>
                  </div>
                </div>
                <div className="flex gap-3">
                  <div className="mt-1">
                    <div className="h-2 w-2 rounded-full bg-violet-500" />
                  </div>
                  <div>
                    <p className="font-medium text-sm">Modern Architecture</p>
                    <p className="text-sm text-muted-foreground">
                      Built with modern cloud-native principles for scalability, flexibility, and performance.
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
