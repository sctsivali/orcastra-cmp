"use client";

import * as React from "react";
import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ChevronUp,
  Layers,
  Cpu,
  HardDrive,
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

export function Sidebar({ className }: SidebarProps) {
  const { sidebarCollapsed: isCollapsed, setSidebarCollapsed: setIsCollapsed, sidebarOpen, setSidebarOpen } = useLayout();
  const [servicesExpanded, setServicesExpanded] = useState(false);
  const [techExpanded, setTechExpanded] = useState(false);
  const [hardwareExpanded, setHardwareExpanded] = useState(false);
  const [softwareExpanded, setSoftwareExpanded] = useState(false);
  const [inventoryExpanded, setInventoryExpanded] = useState(false);
  const [selectedMerchant, setSelectedMerchant] =
    useState<MerchantInfo | null>(null);
  const { theme, setTheme } = useTheme();
  
  const pathname = usePathname();

  // Auto-expand inventory on mobile if on inventory page
  useEffect(() => {
    if (pathname?.startsWith("/inventory") && window.innerWidth < 1024) {
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
      name: "LXD",
      version: "5.x",
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

  // Wrapper component for links to handle mobile close
  const SidebarLink = ({ href, children, ...props }: any) => (
    <Link href={href} onClick={handleLinkClick} {...props}>
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
          "fixed left-0 top-0 z-50 border-r bg-card transition-all duration-300",
          // Height - use 100dvh for mobile compatibility, fallback to 100vh
          "h-[100dvh] h-screen",
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
                src={theme === "light" ? "/orca-logo-black.png" : "/orca-logo-white.png"}
                alt="Orcastra Logo"
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
                    isCollapsed ? "md:justify-center md:px-2" : "justify-start"
                  )}
                  onClick={() => {
                    if (isCollapsed && window.innerWidth >= 1024) {
                      // On large screens with collapsed sidebar, go to link
                      window.location.href = "/inventory";
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
                    
                    {/* Clustering Submenu */}
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Clustering</p>
                      <Button
                        variant={pathname === "/inventory/clustering/members" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                        asChild
                      >
                        <SidebarLink href="/inventory/clustering/members">
                          <Users className="h-4 w-4 mr-2" />
                          Members
                        </SidebarLink>
                      </Button>
                    </div>
                    
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
                          <Network className="h-4 w-4 mr-2" />
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
                    
                    <Button
                      variant={pathname === "/inventory/server" ? "secondary" : "ghost"}
                      className="w-full justify-start text-sm"
                      size="sm"
                      asChild
                    >
                      <SidebarLink href="/inventory/server">
                        <Server className="h-4 w-4 mr-2" />
                        Server
                      </SidebarLink>
                    </Button>
                    
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
                    
                    {/* Permissions Submenu */}
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
                  </div>
                )}
              </div>

              {/* Services Menu */}
              <div>
                <Button
                  variant={pathname === "/service-providers" ? "default" : "ghost"}
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && !sidebarOpen && "md:justify-center md:px-2"
                  )}
                  asChild
                >
                  <SidebarLink href="/service-providers">
                    <Layers className="h-5 w-5" />
                    <span className={cn(
                      "ml-3 flex-1 text-left",
                      isCollapsed && !sidebarOpen && "md:hidden"
                    )}>Services</span>
                  </SidebarLink>
                </Button>
              </div>

              {/* Solutions Menu */}
              <div>
                <Button
                  variant={pathname === "/solutions" ? "default" : "ghost"}
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && !sidebarOpen && "md:justify-center md:px-2"
                  )}
                  asChild
                >
                  <SidebarLink href="/solutions">
                    <HardDrive className="h-5 w-5" />
                    <span className={cn(
                      "ml-3 flex-1 text-left",
                      isCollapsed && !sidebarOpen && "md:hidden"
                    )}>Solutions</span>
                  </SidebarLink>
                </Button>
              </div>

              {/* Technologies Menu */}
              <div>
                <Button
                  variant={pathname === "/technologies" ? "default" : "ghost"}
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && !sidebarOpen && "md:justify-center md:px-2"
                  )}
                  asChild
                >
                  <SidebarLink href="/technologies">
                    <Server className="h-5 w-5" />
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
            {/* Settings Link */}
            <Button
              variant={pathname === "/settings" ? "default" : "ghost"}
              className={cn(
                "w-full justify-start",
                isCollapsed && !sidebarOpen && "md:justify-center md:px-2"
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
