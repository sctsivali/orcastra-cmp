"use client";

import * as React from "react";
import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  Layers,
  Cpu,
  HardDrive,
  Disc,
  Server,
  Package,
  Settings,
  Sun,
  Moon,
  Monitor,
  Menu,
  Box,
  Network,
  Shield,
  Database,
  ImageIcon,
  Activity,
  AlertTriangle,
  Users,
  UserCircle,
  Cog,
  X,
  Blocks,
  Terminal,
  MapPin,
  Globe,
  MonitorPlay,
  UserPlus,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import Image from "next/image";
import { useTheme } from "./theme-provider";
import { useLayout } from "./layout-provider";
import { usePersonalization, useCurrentLogo } from "./personalization-provider";
import { useTerminalStore } from "@/lib/terminal-store";
import { useConsoleStore } from "@/lib/console-store";
import { usePermissions } from "@/hooks/use-permissions";
import { Role } from "@/types/rbac";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { queryKeys } from "@/hooks/use-lxd";

interface SidebarProps {
  className?: string;
}

interface MerchantInfo {
  name: string;
  logo: string;
  website: string;
  description: string;
  services: string[];
  established: string;
}

interface HardwareInfo {
  category: string;
  items: { name: string; value: string }[];
}

interface SoftwareInfo {
  name: string;
  version: string;
  purpose: string;
}

const merchantsData: MerchantInfo[] = [
  {
    name: "Ecobytes",
    logo: "/Logo-Ecobytes-1024x177.webp",
    website: "https://eco-bytes.com/",
    description:
      "Ecobytes is a sustainability-focused technology company providing eco-friendly solutions for businesses. They specialize in green cloud computing and sustainable IT infrastructure.",
    services: [
      "Green Cloud Computing",
      "Sustainable IT Infrastructure",
      "Carbon Footprint Monitoring",
      "Energy-Efficient Solutions",
    ],
    established: "2020",
  },
  {
    name: "JETData.AI",
    logo: "/jetdata-logo.webp",
    website: "https://jetdata.ai/",
    description:
      "Next-generation data management platform with AI-powered capabilities.",
    services: [
      "Data Management Platform",
      "AI-Powered Decision-Making",
      "Workflow Automation",
      "Rapid App Development",
    ],
    established: "1992",
  },
  {
    name: "ELO",
    logo: "/elo-logo.svg",
    website: "https://www.elo.com/id-id.html",
    description:
      "Enterprise content management and digital transformation solutions.",
    services: [
      "Enterprise Content Management",
      "Document Management Systems",
      "Workflow Automation",
      "Digital Transformation",
    ],
    established: "1986",
  },
];

// Terminal menu item with active terminals count
function TerminalMenuItem() {
  const terminals = useTerminalStore((s) => s.terminals);
  const totalCount = terminals.length;
  const store = useTerminalStore.getState();

  if (totalCount === 0) {
    return (
      <Button
        variant="ghost"
        className="w-full justify-start text-sm relative text-muted-foreground"
        size="sm"
        disabled
      >
        <Terminal className="h-4 w-4 mr-2" />
        Terminals
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className="w-full justify-start text-sm relative"
          size="sm"
        >
          <Terminal className="h-4 w-4 mr-2" />
          Terminals
          <Badge 
            variant="secondary" 
            className="ml-auto h-5 min-w-5 px-1.5 text-xs bg-blue-500/20 text-blue-500"
          >
            {totalCount}
          </Badge>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {terminals.map((terminal) => (
          <DropdownMenuItem
            key={terminal.id}
            className="flex items-center justify-between gap-2"
            onClick={() => {
              if (terminal.minimized) {
                store.restoreTerminal(terminal.id);
              } else {
                store.focusTerminal(terminal.id);
              }
            }}
          >
            <div className="flex items-center gap-2 flex-1 truncate">
              <Terminal className="h-4 w-4 shrink-0" />
              <span className="truncate">{terminal.instanceName}</span>
            </div>
            <div className="flex items-center gap-1">
              {terminal.minimized ? (
                <Badge variant="outline" className="text-xs">minimized</Badge>
              ) : (
                <Badge variant="secondary" className="text-xs">open</Badge>
              )}
              <Button
                variant="ghost"
                size="icon"
                className="h-5 w-5"
                onClick={(e) => {
                  e.stopPropagation();
                  store.closeTerminal(terminal.id);
                }}
              >
                <X className="h-3 w-3" />
              </Button>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// Console menu item with active consoles count
function ConsoleMenuItem() {
  const consoles = useConsoleStore((s) => s.consoles);
  const totalCount = consoles.length;
  const store = useConsoleStore.getState();

  if (totalCount === 0) {
    return (
      <Button
        variant="ghost"
        className="w-full justify-start text-sm relative text-muted-foreground"
        size="sm"
        disabled
      >
        <Monitor className="h-4 w-4 mr-2" />
        Consoles
      </Button>
    );
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          className="w-full justify-start text-sm relative"
          size="sm"
        >
          <Monitor className="h-4 w-4 mr-2" />
          Consoles
          <Badge 
            variant="secondary" 
            className="ml-auto h-5 min-w-5 px-1.5 text-xs bg-green-500/20 text-green-500"
          >
            {totalCount}
          </Badge>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {consoles.map((console) => (
          <DropdownMenuItem
            key={console.id}
            className="flex items-center justify-between gap-2"
            onClick={() => {
              if (console.minimized) {
                store.restoreConsole(console.id);
              } else {
                store.focusConsole(console.id);
              }
            }}
          >
            <div className="flex items-center gap-2 flex-1 truncate">
              {console.instanceType === 'virtual-machine' ? (
                <Monitor className="h-4 w-4 shrink-0" />
              ) : (
                <Box className="h-4 w-4 shrink-0" />
              )}
              <span className="truncate">{console.instanceName}</span>
            </div>
            <div className="flex items-center gap-1">
              {console.minimized ? (
                <Badge variant="outline" className="text-xs">minimized</Badge>
              ) : (
                <Badge variant="secondary" className="text-xs">open</Badge>
              )}
              <Button
                variant="ghost"
                size="icon"
                className="h-5 w-5"
                onClick={(e) => {
                  e.stopPropagation();
                  store.closeConsole(console.id);
                }}
              >
                <X className="h-3 w-3" />
              </Button>
            </div>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function Sidebar({ className }: SidebarProps) {
  const { sidebarCollapsed: isCollapsed, setSidebarCollapsed: setIsCollapsed, sidebarOpen, setSidebarOpen } = useLayout();
  const pathname = usePathname();
  const router = useRouter();
  // open the submenu you are standing in, on first paint. usePathname() is available
  // during SSR in the App Router, so seeding from it is hydration-safe.
  const [inventoryExpanded, setInventoryExpanded] = useState(
    () => pathname?.startsWith("/inventory") ?? false
  );
  const [selectedMerchant, setSelectedMerchant] =
    useState<MerchantInfo | null>(null);
  const { theme, setTheme } = useTheme();
  const { settings } = usePersonalization();
  const currentLogo = useCurrentLogo(theme);
  const { isAdmin, isPartner, isTenant } = usePermissions();

  // keep it open when navigating deeper into /inventory, at any viewport
  useEffect(() => {
    if (pathname?.startsWith("/inventory")) {
      setInventoryExpanded(true);
    }
  }, [pathname]);
  
  // Save collapsed state to localStorage
  const handleToggleCollapse = () => {
    const newState = !isCollapsed;
    setIsCollapsed(newState);
    localStorage.setItem("sidebar-collapsed", String(newState));
  };

  // Close sidebar on mobile when clicking a link
  const handleLinkClick = () => {
    if (window.innerWidth < 768) { // md breakpoint
      setSidebarOpen(false);
    }
  };

  const hardwareData: HardwareInfo[] = [
    {
      category: "CPU",
      items: [
        { name: "Architecture", value: "x86_64" },
        { name: "Total Cores", value: "Variable per node" },
        { name: "Thread per Core", value: "1-2" },
      ],
    },
    {
      category: "Storage",
      items: [
        { name: "Type", value: "ZFS/Dir/BTRFS" },
        { name: "Backend", value: "Local & Network" },
      ],
    },
    {
      category: "Memory",
      items: [
        { name: "Type", value: "DDR4/DDR5" },
        { name: "Total", value: "Variable per node" },
      ],
    },
    {
      category: "Network",
      items: [
        { name: "Interface", value: "Ethernet" },
        { name: "Protocol", value: "TCP/IP" },
      ],
    },
  ];

  // Software stack
  const softwareData: SoftwareInfo[] = [
    {
      name: "Hypervisor",
      version: "Detected",
      purpose: "Container & VM Management",
    },
    {
      name: "Linux Kernel",
      version: "6.x",
      purpose: "Operating System Core",
    },
    {
      name: "ZFS",
      version: "2.x",
      purpose: "Filesystem & Storage Management",
    },
    {
      name: "QEMU",
      version: "8.x",
      purpose: "Virtual Machine Emulation",
    },
    {
      name: "OVS",
      version: "3.x",
      purpose: "Network Virtualization",
    },
  ];

  // route → prefetch mapping: warm React Query cache on sidebar hover
  const queryClient = useQueryClient();
  const prefetchMap: Record<string, () => void> = React.useMemo(() => ({
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
      queryClient.prefetchQuery({ queryKey: queryKeys.nodes, queryFn: api.nodes.status, staleTime: 10_000 });
      queryClient.prefetchQuery({ queryKey: ["instances", undefined], queryFn: () => api.instances.list(), staleTime: 30_000 });
    },
  }), [queryClient]);

  // Wrapper component for links to handle mobile close + prefetch on hover
  const SidebarLink = ({ href, children, ...props }: any) => (
    <Link
      href={href}
      onClick={handleLinkClick}
      onMouseEnter={() => prefetchMap[href]?.()}
      {...props}
    >
      {children}
    </Link>
  );

  return (
    <>
      {/* Overlay - show when sidebar is open */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-[45] bg-black/50 backdrop-blur-sm"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside
        className={cn(
          "fixed left-0 top-0 z-50 border-r transition-all duration-300",
          // Apply custom sidebar styling only when user has customized the sidebar background
          settings.colors.useCustomSidebarBg && "sidebar-custom-bg",
          // When not customized, use default theme colors (bg-card for consistency)
          !settings.colors.useCustomSidebarBg && "bg-card",
          "h-[100dvh]",
          // Ensure sidebar stays on top during scroll with proper positioning
          "overscroll-contain",
          // When opened via hamburger: always full width, always visible
          sidebarOpen && "w-64 translate-x-0",
          // When not opened via hamburger: hide on mobile, show on desktop
          !sidebarOpen && "-translate-x-full md:translate-x-0",
          // Desktop collapsed/expanded (only when NOT opened via hamburger)
          !sidebarOpen && isCollapsed && "md:w-16",
          !sidebarOpen && !isCollapsed && "md:w-64",
          className
        )}
        style={settings.colors.useCustomSidebarBg ? { backgroundColor: settings.colors.sidebarBgColor || "#ffffff" } : undefined}
      >
        {/* Inner flex container for layout */}
        <div className="flex flex-col h-full overflow-hidden overscroll-contain">
        {/* Header with toggle */}
        <div className="flex h-16 md:h-20 items-center justify-between border-b px-3 md:px-2 shrink-0">
            <SidebarLink
              href="/"
              className={cn(
                "flex items-center justify-center rounded-lg transition-all hover:bg-accent",
                // When opened via hamburger, always show expanded
                sidebarOpen ? "flex-1 px-3 gap-3" : (isCollapsed ? "w-10 h-10 md:w-12 md:h-12" : "flex-1 px-3 gap-3")
              )}
            >
              <Image
                src={currentLogo}
                alt={`${settings.branding.appName} Logo`}
                width={120}
                height={120}
                className={cn(
                  "cursor-pointer transition-all duration-300 object-contain",
                  // When opened via hamburger or not collapsed: full size
                  // When collapsed (desktop only): small icon
                  sidebarOpen || !isCollapsed
                    ? "w-auto h-8 md:h-10 lg:h-12"
                    : "w-8 h-8 md:w-10 md:h-10"
                )}
                unoptimized={currentLogo.startsWith('data:')}
              />
            </SidebarLink>
            {/* Close button - show when sidebar is opened via menu button */}
            {sidebarOpen && (
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setSidebarOpen(false)}
              >
                <X className="h-5 w-5" />
              </Button>
            )}
            {/* Desktop collapse button - show when sidebar is not opened via menu */}
            {!sidebarOpen && (
              <Button
                variant="ghost"
                size="icon"
                onClick={handleToggleCollapse}
                className={cn(isCollapsed && "ml-1", "hidden md:flex")}
              >
                {isCollapsed ? (
                  <ChevronRight className="h-4 w-4" />
                ) : (
                  <ChevronLeft className="h-4 w-4" />
                )}
              </Button>
            )}
          </div>

          {/* Menu Items - ScrollArea takes remaining space */}
          <ScrollArea className="flex-1">
            <div className="px-3 py-4 space-y-2">
              {/* Inventory Menu with Submenu */}
              <div>
                <Button
                  variant={pathname?.startsWith("/inventory") ? "default" : "ghost"}
                  className={cn(
                    "w-full",
                    isCollapsed ? "md:justify-center md:px-2" : "justify-start",
                    pathname?.startsWith("/inventory") && "sidebar-active"
                  )}
                  onClick={() => {
                    if (isCollapsed && window.innerWidth >= 1024) {
                      // important: soft nav. A document navigation here fires
                      // beforeunload, and the console page beacons kill-all-sessions
                      // on unload, so this used to kill the operator's live consoles.
                      router.push("/inventory");
                      handleLinkClick();
                    } else {
                      // Otherwise toggle dropdown
                      setInventoryExpanded(!inventoryExpanded);
                    }
                  }}
                >
                  <Package className="h-5 w-5" />
                  <span className={cn(
                    "ml-3 flex-1 text-left",
                    isCollapsed && !sidebarOpen && "md:hidden"
                  )}>Inventory</span>
                  <span className={cn(
                    isCollapsed && !sidebarOpen && "md:hidden"
                  )}>
                    {inventoryExpanded ? (
                      <ChevronUp className="h-4 w-4" />
                    ) : (
                      <ChevronDown className="h-4 w-4" />
                    )}
                  </span>
                </Button>
                
                {/* Inventory Submenu */}
                {inventoryExpanded && (
                  <div className={cn(
                    "mt-2 ml-4 space-y-1 border-l-2 border-border pl-2",
                    isCollapsed && !sidebarOpen && "md:hidden"
                  )}>
                    <Button
                      variant={pathname === "/inventory" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm font-semibold"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory">
                        <Package className="h-4 w-4 mr-2" />
                        Overview
                      </SidebarLink>
                    </Button>
                    
                    <Button
                      variant={pathname === "/inventory/monitoring" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/monitoring">
                        <Activity className="h-4 w-4 mr-2" />
                        Monitoring
                      </SidebarLink>
                    </Button>

                    
                    {/* Clusters - Partner+ only */}
                    {isPartner && (
                    <Button
                      variant={pathname === "/inventory/clusters" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/clusters">
                        <Server className="h-4 w-4 mr-2" />
                        Clusters
                      </SidebarLink>
                    </Button>
                    )}
                    
                    {/* Clustering Submenu - Partner+ only */}
                    {isPartner && (
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Clustering</p>
                      <Button
                        variant={pathname === "/inventory/clustering/members" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/clustering/members">
                          <UserPlus className="h-4 w-4 mr-2" />
                          Members
                        </SidebarLink>
                      </Button>
                    </div>
                    )}
                    
                    <Button
                      variant={pathname === "/inventory/instances" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/instances">
                        <Box className="h-4 w-4 mr-2" />
                        Instances
                      </SidebarLink>
                    </Button>
                    
                    {/* Sessions group — Sessions page + Terminals/Consoles as children */}
                    {isTenant && (
                    <div>
                      <Button
                        variant={pathname === "/inventory/sessions" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/sessions">
                          <MonitorPlay className="h-4 w-4 mr-2" />
                          Sessions
                        </SidebarLink>
                      </Button>
                      <div className="pl-4">
                        <TerminalMenuItem />
                        <ConsoleMenuItem />
                      </div>
                    </div>
                    )}
                    
                    <Button
                      variant={pathname === "/inventory/profiles" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/profiles">
                        <Settings className="h-4 w-4 mr-2" />
                        Profiles
                      </SidebarLink>
                    </Button>
                    
                    {/* Networking Submenu */}
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Networking</p>
                      <Button
                        variant={pathname === "/inventory/networks" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/networks">
                          <Network className="h-4 w-4 mr-2" />
                          Networks
                        </SidebarLink>
                      </Button>
                      
                      <Button
                        variant={pathname === "/inventory/network-acls" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/network-acls">
                          <Shield className="h-4 w-4 mr-2" />
                          ACLs
                        </SidebarLink>
                      </Button>
                      
                      <Button
                        variant={pathname === "/inventory/ipam" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/ipam">
                          <Globe className="h-4 w-4 mr-2" />
                          IPAM
                        </SidebarLink>
                      </Button>
                    </div>
                    
                    {/* Storage Submenu */}
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Storage</p>
                      <Button
                        variant={pathname === "/inventory/storage-pools" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/storage-pools">
                          <Database className="h-4 w-4 mr-2" />
                          Pools
                        </SidebarLink>
                      </Button>
                      
                      <Button
                        variant={pathname === "/inventory/storage-volumes" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/storage-volumes">
                          <HardDrive className="h-4 w-4 mr-2" />
                          Volumes
                        </SidebarLink>
                      </Button>

                      <Button
                        variant={pathname === "/inventory/iso-images" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/iso-images">
                          <Disc className="h-4 w-4 mr-2" />
                          ISO Images
                        </SidebarLink>
                      </Button>
                    </div>
                    
                    <Button
                      variant={pathname === "/inventory/images" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/images">
                        <ImageIcon className="h-4 w-4 mr-2" />
                        Images
                      </SidebarLink>
                    </Button>
                    
                    {/* Server - Partner+ only */}
                    {isPartner && (
                    <Button
                      variant={pathname === "/inventory/server" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/server">
                        <Cpu className="h-4 w-4 mr-2" />
                        Server
                      </SidebarLink>
                    </Button>
                    )}
                    
                    {/* Operations - Partner+ only */}
                    {isPartner && (
                    <Button
                      variant={pathname === "/inventory/operations" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/operations">
                        <Cog className="h-4 w-4 mr-2" />
                        Operations
                      </SidebarLink>
                    </Button>
                    )}

                    {/* Warnings - Partner+ only */}
                    {isPartner && (
                    <Button
                      variant={pathname === "/inventory/warnings" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/warnings">
                        <AlertTriangle className="h-4 w-4 mr-2" />
                        Warnings
                      </SidebarLink>
                    </Button>
                    )}
                    
                    {/* Permissions Submenu - Admin+Partner */}
                    {isPartner && (
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Permissions</p>
                      <Button
                        variant={pathname === "/inventory/identities" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/identities">
                          <UserCircle className="h-4 w-4 mr-2" />
                          Identities
                        </SidebarLink>
                      </Button>
                      
                      <Button
                        variant={pathname === "/inventory/groups" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/groups">
                          <Users className="h-4 w-4 mr-2" />
                          Groups
                        </SidebarLink>
                      </Button>
                    </div>
                    )}
                  </div>
                )}
              </div>

              {/* Regions Menu */}
              <div>
                <Button
                  variant={pathname === "/regions" ? "default" : "ghost"}
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && !sidebarOpen && "md:justify-center md:px-2",
                    pathname === "/regions" && "sidebar-active"
                  )}
                  asChild
                >
                  <SidebarLink href="/regions">
                    <MapPin className="h-5 w-5" />
                    <span className={cn(
                      "ml-3 flex-1 text-left",
                      isCollapsed && !sidebarOpen && "md:hidden"
                    )}>Regions</span>
                  </SidebarLink>
                </Button>
              </div>

              {/* Technologies Menu */}
              <div>
                <Button
                  variant={pathname === "/technologies" ? "default" : "ghost"}
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && !sidebarOpen && "md:justify-center md:px-2",
                    pathname === "/technologies" && "sidebar-active"
                  )}
                  asChild
                >
                  <SidebarLink href="/technologies">
                    <Blocks className="h-5 w-5" />
                    <span className={cn(
                      "ml-3 flex-1 text-left",
                      isCollapsed && !sidebarOpen && "md:hidden"
                    )}>Technologies</span>
                  </SidebarLink>
                </Button>
              </div>
            </div>
          </ScrollArea>

          {/* Footer with Settings and Theme Switcher */}
          <div className="border-t p-3 space-y-2 shrink-0">
            {/* Settings Link - All authenticated users */}
            <Button
              variant={pathname === "/settings" ? "default" : "ghost"}
              className={cn(
                "w-full justify-start",
                isCollapsed && !sidebarOpen && "md:justify-center md:px-2",
                pathname === "/settings" && "sidebar-active"
              )}
              asChild
            >
              <SidebarLink href="/settings">
                <Settings className="h-5 w-5" />
                <span className={cn(
                  "ml-3",
                  isCollapsed && !sidebarOpen && "md:hidden"
                )}>Settings</span>
              </SidebarLink>
            </Button>

            {/* Theme Switcher */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && !sidebarOpen && "md:justify-center md:px-2"
                  )}
                >
                  {theme === "light" ? (
                    <Sun className="h-5 w-5" />
                  ) : theme === "dark" ? (
                    <Moon className="h-5 w-5" />
                  ) : (
                    <Monitor className="h-5 w-5" />
                  )}
                  <span className={cn(
                    "ml-3",
                    isCollapsed && !sidebarOpen && "md:hidden"
                  )}>Theme</span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" side="top" className="w-48">
                <DropdownMenuItem onClick={() => setTheme("light")}>
                  <Sun className="mr-2 h-4 w-4" />
                  Light Mode
                  {theme === "light" && <span className="ml-auto">✓</span>}
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setTheme("dark")}>
                  <Moon className="mr-2 h-4 w-4" />
                  Dark Mode
                  {theme === "dark" && <span className="ml-auto">✓</span>}
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setTheme("system")}>
                  <Monitor className="mr-2 h-4 w-4" />
                  System
                  {theme === "system" && <span className="ml-auto">✓</span>}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </aside>

      {/* Merchant Detail Dialog */}
      <Dialog
        open={selectedMerchant !== null}
        onOpenChange={() => setSelectedMerchant(null)}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-4">
              {selectedMerchant && (
                <>
                  <Image
                    src={selectedMerchant.logo}
                    alt={selectedMerchant.name}
                    width={120}
                    height={40}
                    className="object-contain"
                  />
                  <span>{selectedMerchant.name}</span>
                </>
              )}
            </DialogTitle>
            <DialogDescription>
              {selectedMerchant?.description}
            </DialogDescription>
          </DialogHeader>

          {selectedMerchant && (
            <div className="space-y-4">
              <div>
                <h4 className="mb-2 font-semibold">Website</h4>
                <a
                  href={selectedMerchant.website}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-primary hover:underline"
                >
                  {selectedMerchant.website}
                </a>
              </div>

              <div>
                <h4 className="mb-2 font-semibold">About</h4>
                <p className="text-sm text-muted-foreground">
                  Established: {selectedMerchant.established}
                </p>
              </div>

              <div>
                <h4 className="mb-2 font-semibold">Services Offered</h4>
                <div className="flex flex-wrap gap-2">
                  {selectedMerchant.services.map((service) => (
                    <Badge key={service} variant="secondary">
                      {service}
                    </Badge>
                  ))}
                </div>
              </div>

              <div className="rounded-lg border bg-muted/50 p-4">
                <p className="text-sm text-muted-foreground">
                  Learn more about {selectedMerchant.name} and their
                  sustainability initiatives at their{" "}
                  <a
                    href="https://eco-bytes.com/about-us/"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-primary hover:underline"
                  >
                    About Us page
                  </a>
                  .
                </p>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
