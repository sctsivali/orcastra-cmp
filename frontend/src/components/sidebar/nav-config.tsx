import {
  LayoutGrid,
  Activity,
  Box,
  MonitorPlay,
  Terminal,
  Monitor,
  SlidersHorizontal,
  Image as ImageIcon,
  Network,
  Shield,
  Globe,
  Database,
  HardDrive,
  Disc,
  Server,
  Boxes,
  Cpu,
  Cog,
  AlertTriangle,
  UserCircle,
  Users,
  MapPin,
  Blocks,
  History,
  ArrowLeftRight,
} from "lucide-react";
import type { QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { queryKeys } from "@/hooks/use-lxd";
import type { NavGroup, NavItem, PermissionFlags, RoleGate } from "./types";

// Single source of truth for the sidebar information architecture. Pinned top-level
// destinations plus titled groups (Compute / Networking / Storage / Cluster / Access /
// Platform). Gates are data: the renderer never sees role logic, it receives an
// already-filtered tree.
//
// note: unlike orcastra-mini there is no deploymentMode gate here. NEXT_PUBLIC_AUTH_MODE
// is not a variable this app defines, and next.config.js whitelists only two public vars,
// so the gate would evaluate false forever. This deployment is OIDC-only.
export const NAV_GROUPS: NavGroup[] = [
  {
    id: "top",
    items: [
      { id: "overview", label: "Overview", href: "/inventory", icon: LayoutGrid },
      { id: "monitoring", label: "Monitoring", href: "/inventory/monitoring", icon: Activity },
    ],
  },
  {
    id: "compute",
    label: "Compute",
    icon: Box,
    items: [
      { id: "instances", label: "Instances", href: "/inventory/instances", icon: Box },
      { id: "sessions", label: "Sessions", href: "/inventory/sessions", icon: MonitorPlay, role: "tenant" },
      { id: "terminals", label: "Terminals", icon: Terminal, role: "tenant", render: "terminal-menu", indent: true },
      { id: "consoles", label: "Consoles", icon: Monitor, role: "tenant", render: "console-menu", indent: true },
      { id: "profiles", label: "Profiles", href: "/inventory/profiles", icon: SlidersHorizontal },
      { id: "images", label: "Images", href: "/inventory/images", icon: ImageIcon },
      // Operator restore points. Partner-gated because this console manages maintenance
      // snapshots, not the metered snapshot product a tenant buys upstream.
      { id: "snapshots", label: "Snapshots", href: "/inventory/snapshots", icon: History, role: "partner" },
    ],
  },
  {
    id: "networking",
    label: "Networking",
    icon: Network,
    items: [
      { id: "networks", label: "Networks", href: "/inventory/networks", icon: Network },
      { id: "acls", label: "ACLs", href: "/inventory/network-acls", icon: Shield },
      { id: "ipam", label: "IPAM", href: "/inventory/ipam", icon: Globe },
    ],
  },
  {
    id: "storage",
    label: "Storage",
    icon: Database,
    items: [
      { id: "pools", label: "Pools", href: "/inventory/storage-pools", icon: Database },
      { id: "volumes", label: "Volumes", href: "/inventory/storage-volumes", icon: HardDrive },
      // ISO Images reads from the storage-volumes query with no arguments, so it warms the
      // same cache key rather than needing a prefetch entry of its own.
      {
        id: "iso-images",
        label: "ISO Images",
        href: "/inventory/iso-images",
        icon: Disc,
        prefetchKey: "/inventory/storage-volumes",
      },
    ],
  },
  {
    id: "cluster",
    label: "Cluster",
    icon: Server,
    role: "partner",
    items: [
      { id: "clusters", label: "Clusters", href: "/inventory/clusters", icon: Server },
      { id: "members", label: "Members", href: "/inventory/clustering/members", icon: Boxes },
      { id: "server", label: "Server", href: "/inventory/server", icon: Cpu },
      { id: "operations", label: "Operations", href: "/inventory/operations", icon: Cog },
      { id: "migrations", label: "Migrations", href: "/inventory/migrations", icon: ArrowLeftRight },
      { id: "warnings", label: "Warnings", href: "/inventory/warnings", icon: AlertTriangle },
    ],
  },
  {
    id: "access",
    label: "Access",
    icon: Users,
    role: "partner",
    items: [
      { id: "identities", label: "Identities", href: "/inventory/identities", icon: UserCircle },
      { id: "groups", label: "Groups", href: "/inventory/groups", icon: Users },
    ],
  },
  {
    id: "platform",
    label: "Platform",
    icon: Blocks,
    items: [
      { id: "regions", label: "Regions", href: "/regions", icon: MapPin },
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

export function itemVisible(item: NavItem, perms: PermissionFlags): boolean {
  return roleAllowed(item.role, perms);
}

// Returns the IA filtered by role; groups with no visible items are dropped.
export function filterGroups(groups: NavGroup[], perms: PermissionFlags): NavGroup[] {
  return groups
    .filter((g) => roleAllowed(g.role, perms))
    .map((g) => ({ ...g, items: g.items.filter((i) => itemVisible(i, perms)) }))
    .filter((g) => g.items.length > 0);
}

// route -> prefetch: warm the React Query cache on sidebar hover. Co-located with the
// config so prefetchKey values stay in sync with the routes they warm.
export function buildPrefetchMap(
  queryClient: QueryClient,
  actorScope: string,
  canViewNodeInventory: boolean,
): Record<string, () => void> {
  return {
    "/inventory": () => {
      queryClient.prefetchQuery({ queryKey: queryKeys.instancesFor(actorScope, undefined), queryFn: () => api.instances.list(), staleTime: 30_000 });
      queryClient.prefetchQuery({ queryKey: queryKeys.allProjectsFor(actorScope), queryFn: () => api.projects.listAll(), staleTime: 180_000 });
      queryClient.prefetchQuery({ queryKey: queryKeys.clustersFor(actorScope, false), queryFn: () => api.clusters.list(false), staleTime: 120_000 });
    },
    "/inventory/instances": () => {
      queryClient.prefetchQuery({ queryKey: queryKeys.instancesFor(actorScope, undefined), queryFn: () => api.instances.list(), staleTime: 30_000 });
    },
    "/inventory/monitoring": () => {
      queryClient.prefetchQuery({
        queryKey: ["monitoring", "dashboard", actorScope],
        queryFn: () => api.monitoring.getDashboard(),
        staleTime: 0,
      });
    },
    "/inventory/networks": () => {
      queryClient.prefetchQuery({ queryKey: ["networks", undefined], queryFn: () => api.networks.list(), staleTime: 120_000 });
    },
    "/inventory/storage-pools": () => {
      queryClient.prefetchQuery({ queryKey: ["storagePools", undefined], queryFn: async () => {
        const [pools, resources] = await Promise.all([api.storagePools.list() as Promise<any>, api.storagePools.getResourcesAll() as Promise<any>]);
        return { ...pools, resources: resources?.resources || {} };
      }, staleTime: 120_000 });
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
    "/": () => {
      if (canViewNodeInventory) {
        queryClient.prefetchQuery({
          queryKey: queryKeys.nodesFor(actorScope),
          queryFn: () => api.nodes.status(),
          staleTime: 30_000,
        });
      } else {
        queryClient.prefetchQuery({
          queryKey: queryKeys.nodeAvailabilityFor(actorScope),
          queryFn: () => api.nodes.availability(),
          staleTime: 30_000,
        });
      }
      queryClient.prefetchQuery({ queryKey: queryKeys.instancesFor(actorScope, undefined), queryFn: () => api.instances.list(), staleTime: 30_000 });
    },
  };
}
