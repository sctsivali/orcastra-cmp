import {
  LayoutGrid,
  Activity,
  Box,
  MonitorPlay,
  Terminal,
  Monitor,
  SlidersHorizontal,
  ImageIcon,
  Network,
  Shield,
  Globe,
  Database,
  HardDrive,
  Server,
  Boxes,
  Cpu,
  Cog,
  AlertTriangle,
  UserCircle,
  Users,
  UserPlus,
  ScrollText,
  ShieldCheck,
  MapPin,
  Blocks,
} from "lucide-react";
import type { QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { queryKeys } from "@/hooks/use-lxd";
import type { NavGroup, NavItem, PermissionFlags, DeploymentGate, RoleGate } from "./types";

// Single source of truth for the sidebar information architecture and the Cmd+K palette.
// Flat top-level destinations + a few titled groups (Compute / Networking / Storage /
// Cluster / Access / Administration / Platform). Gates are data; the renderer never
// sees role logic - it receives an already-filtered tree.
export const NAV_GROUPS: NavGroup[] = [
  {
    id: "top",
    items: [
      // The landing page (/) is reached via the logo, not a nav item. The "/" prefetch
      // entry in buildPrefetchMap is kept so the logo link still warms the home data.
      { id: "overview", label: "Overview", href: "/inventory", icon: LayoutGrid, prefetchKey: "/inventory" },
      { id: "monitoring", label: "Monitoring", href: "/inventory/monitoring", icon: Activity, prefetchKey: "/inventory/monitoring" },
    ],
  },
  {
    id: "compute",
    label: "Compute",
    icon: Box,
    items: [
      { id: "instances", label: "Instances", href: "/inventory/instances", icon: Box, prefetchKey: "/inventory/instances" },
      { id: "sessions", label: "Sessions", href: "/inventory/sessions", icon: MonitorPlay },
      { id: "terminals", label: "Terminals", icon: Terminal, render: "terminal-menu" },
      { id: "consoles", label: "Consoles", icon: Monitor, render: "console-menu" },
      { id: "profiles", label: "Profiles", href: "/inventory/profiles", icon: SlidersHorizontal, prefetchKey: "/inventory/profiles" },
      { id: "images", label: "Images", href: "/inventory/images", icon: ImageIcon, prefetchKey: "/inventory/images" },
    ],
  },
  {
    id: "networking",
    label: "Networking",
    icon: Network,
    items: [
      { id: "networks", label: "Networks", href: "/inventory/networks", icon: Network, prefetchKey: "/inventory/networks" },
      { id: "acls", label: "ACLs", href: "/inventory/network-acls", icon: Shield, prefetchKey: "/inventory/network-acls" },
      { id: "ipam", label: "IPAM", href: "/inventory/ipam", icon: Globe, prefetchKey: "/inventory/ipam" },
    ],
  },
  {
    id: "storage",
    label: "Storage",
    icon: Database,
    items: [
      { id: "pools", label: "Pools", href: "/inventory/storage-pools", icon: Database, prefetchKey: "/inventory/storage-pools" },
      { id: "volumes", label: "Volumes", href: "/inventory/storage-volumes", icon: HardDrive, prefetchKey: "/inventory/storage-volumes" },
    ],
  },
  {
    id: "cluster",
    label: "Cluster",
    role: "partner",
    icon: Server,
    items: [
      { id: "clusters", label: "Clusters", href: "/inventory/clusters", icon: Server },
      { id: "members", label: "Members", href: "/inventory/clustering/members", icon: Boxes },
      { id: "server", label: "Server", href: "/inventory/server", icon: Cpu },
      { id: "operations", label: "Operations", href: "/inventory/operations", icon: Cog },
      { id: "warnings", label: "Warnings", href: "/inventory/warnings", icon: AlertTriangle },
    ],
  },
  {
    id: "access",
    label: "Access",
    role: "partner",
    icon: Users,
    items: [
      { id: "identities", label: "Identities", href: "/inventory/identities", icon: UserCircle },
      { id: "groups", label: "Groups", href: "/inventory/groups", icon: Users },
    ],
  },
  {
    id: "administration",
    label: "Administration",
    role: "admin",
    deploymentMode: "client-cert",
    icon: ShieldCheck,
    items: [
      { id: "local-identities", label: "Local Identities", href: "/admin/local-identities", icon: UserPlus },
      { id: "audit-log", label: "Audit Log", href: "/admin/audit-log", icon: ScrollText },
    ],
  },
  {
    id: "platform",
    label: "Platform",
    icon: Blocks,
    items: [
      { id: "regions", label: "Regions", href: "/regions", icon: MapPin, prefetchKey: "/regions" },
      { id: "technologies", label: "Technologies", href: "/technologies", icon: Blocks },
    ],
  },
];

function roleAllowed(role: RoleGate | undefined, perms: PermissionFlags): boolean {
  if (!role) return true;
  if (role === "admin") return perms.isAdmin;
  if (role === "partner") return perms.isPartner;
  return perms.isTenant;
}

function deploymentAllowed(mode: DeploymentGate | undefined): boolean {
  if (!mode) return true;
  return process.env.NEXT_PUBLIC_AUTH_MODE === mode;
}

export function itemVisible(item: NavItem, perms: PermissionFlags): boolean {
  return roleAllowed(item.role, perms) && deploymentAllowed(item.deploymentMode);
}

// Returns the IA filtered by role + deployment mode; groups with no visible items are dropped.
export function filterGroups(groups: NavGroup[], perms: PermissionFlags): NavGroup[] {
  return groups
    .filter((g) => roleAllowed(g.role, perms) && deploymentAllowed(g.deploymentMode))
    .map((g) => ({ ...g, items: g.items.filter((i) => itemVisible(i, perms)) }))
    .filter((g) => g.items.length > 0);
}

// route -> prefetch: warm the React Query cache on sidebar hover. Co-located with the
// config so prefetchKey values stay in sync with the routes they warm.
export function buildPrefetchMap(queryClient: QueryClient): Record<string, () => void> {
  return {
    "/": () => {
      queryClient.prefetchQuery({ queryKey: queryKeys.nodes, queryFn: api.nodes.status, staleTime: 10_000 });
      queryClient.prefetchQuery({ queryKey: ["instances", undefined], queryFn: () => api.instances.list(), staleTime: 30_000 });
    },
    "/inventory": () => {
      queryClient.prefetchQuery({ queryKey: ["instances", undefined], queryFn: () => api.instances.list(), staleTime: 30_000 });
      queryClient.prefetchQuery({ queryKey: queryKeys.allProjects, queryFn: () => api.projects.listAll(), staleTime: 180_000 });
      queryClient.prefetchQuery({ queryKey: ["clusters", { fresh: false }], queryFn: () => api.clusters.list(false), staleTime: 120_000 });
    },
    "/inventory/instances": () => {
      queryClient.prefetchQuery({ queryKey: ["instances", undefined], queryFn: () => api.instances.list(), staleTime: 30_000 });
    },
    "/inventory/monitoring": () => {
      queryClient.prefetchQuery({ queryKey: ["monitoring", "dashboard"], queryFn: () => api.monitoring.getDashboard(), staleTime: 0 });
    },
    "/inventory/networks": () => {
      queryClient.prefetchQuery({ queryKey: ["networks", undefined], queryFn: () => api.networks.list(), staleTime: 120_000 });
    },
    "/inventory/storage-pools": () => {
      queryClient.prefetchQuery({
        queryKey: ["storagePools", undefined],
        queryFn: async () => {
          const [pools, resources] = await Promise.all([
            api.storagePools.list() as Promise<any>,
            api.storagePools.getResourcesAll() as Promise<any>,
          ]);
          return { ...pools, resources: resources?.resources || {} };
        },
        staleTime: 120_000,
      });
    },
    "/inventory/storage-volumes": () => {
      queryClient.prefetchQuery({ queryKey: ["storageVolumes", undefined, undefined], queryFn: () => api.storageVolumes.list(), staleTime: 120_000 });
    },
    "/inventory/images": () => {
      queryClient.prefetchQuery({ queryKey: ["images", undefined], queryFn: () => api.images.listAll(), staleTime: 120_000 });
    },
    "/inventory/profiles": () => {
      queryClient.prefetchQuery({ queryKey: ["profiles", undefined], queryFn: () => api.profiles.list(), staleTime: 120_000 });
    },
    "/inventory/ipam": () => {
      queryClient.prefetchQuery({ queryKey: ["ipam", undefined], queryFn: () => api.ipam.list(), staleTime: 120_000 });
    },
    "/inventory/network-acls": () => {
      queryClient.prefetchQuery({ queryKey: ["networkACLs", undefined], queryFn: () => api.networkACLs.list(), staleTime: 120_000 });
    },
    "/regions": () => {
      queryClient.prefetchQuery({ queryKey: ["regions", "overview"], queryFn: () => api.regions.getOverview(), staleTime: 60_000 });
    },
  };
}
