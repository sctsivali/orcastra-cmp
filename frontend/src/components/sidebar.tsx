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
  const { sidebarCollapsed: isCollapsed, setSidebarCollapsed: setIsCollapsed } = useLayout();
  const [servicesExpanded, setServicesExpanded] = useState(false);
  const [techExpanded, setTechExpanded] = useState(false);
  const [hardwareExpanded, setHardwareExpanded] = useState(false);
  const [softwareExpanded, setSoftwareExpanded] = useState(false);
  const [inventoryExpanded, setInventoryExpanded] = useState(false);
  const [selectedMerchant, setSelectedMerchant] =
    useState<MerchantInfo | null>(null);
  const { theme, setTheme } = useTheme();
  
  // Save collapsed state to localStorage
  const handleToggleCollapse = () => {
    const newState = !isCollapsed;
    setIsCollapsed(newState);
    localStorage.setItem("sidebar-collapsed", String(newState));
  };

  // Hardware data from cluster resources
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

  const pathname = usePathname();

  return (
    <>
      <aside
        className={cn(
          "fixed left-0 top-0 z-40 h-screen border-r bg-card transition-all duration-300",
          isCollapsed ? "w-16" : "w-64",
          className
        )}
      >
        <div className="flex h-full flex-col">
          {/* Header with toggle */}
          <div className="flex h-20 items-center justify-between border-b px-2">
            <Link 
              href="/" 
              className={cn(
                "hover:scale-105 transition-transform flex items-center justify-center",
                isCollapsed ? "w-full" : "flex-1"
              )}
            >
              <Image
                src={theme === "light" ? "/orca-logo-black.png" : "/orca-logo-white.png"}
                alt="Orcastra Logo"
                width={isCollapsed ? 96 : 48}
                height={isCollapsed ? 96 : 48}
                className="cursor-pointer transition-all duration-300"
              />
            </Link>
            <Button
              variant="ghost"
              size="icon"
              onClick={handleToggleCollapse}
              className={cn(isCollapsed && "ml-1")}
            >
              {isCollapsed ? (
                <ChevronRight className="h-4 w-4" />
              ) : (
                <ChevronLeft className="h-4 w-4" />
              )}
            </Button>
          </div>

          {/* Menu Items */}
          <ScrollArea className="flex-1 px-3 py-4">
            <div className="space-y-2">
              {/* Inventory Menu with Submenu */}
              <div>
                {isCollapsed ? (
                  <Link href="/inventory">
                    <Button
                      variant={pathname?.startsWith("/inventory") ? "default" : "ghost"}
                      className={cn(
                        "w-full justify-center px-2"
                      )}
                    >
                      <Package className="h-5 w-5" />
                    </Button>
                  </Link>
                ) : (
                  <Button
                    variant={pathname?.startsWith("/inventory") ? "default" : "ghost"}
                    className="w-full justify-start"
                    onClick={() => setInventoryExpanded(!inventoryExpanded)}
                  >
                    <Package className="h-5 w-5" />
                    <span className="ml-3 flex-1 text-left">Inventory</span>
                    {inventoryExpanded ? (
                      <ChevronUp className="h-4 w-4" />
                    ) : (
                      <ChevronDown className="h-4 w-4" />
                    )}
                  </Button>
                )}
                
                {/* Inventory Submenu */}
                {!isCollapsed && inventoryExpanded && (
                  <div className="mt-2 ml-4 space-y-1 border-l-2 border-border pl-2">
                    <Link href="/inventory">
                      <Button
                        variant={pathname === "/inventory" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm font-semibold"
                        size="sm"
                      >
                        <Package className="h-4 w-4 mr-2" />
                        Overview
                      </Button>
                    </Link>
                    
                    <Link href="/inventory/instances">
                      <Button
                        variant={pathname === "/inventory/instances" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                      >
                        <Box className="h-4 w-4 mr-2" />
                        Instances
                      </Button>
                    </Link>
                    
                    <Link href="/inventory/profiles">
                      <Button
                        variant={pathname === "/inventory/profiles" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                      >
                        <Settings className="h-4 w-4 mr-2" />
                        Profiles
                      </Button>
                    </Link>
                    
                    {/* Networking Submenu */}
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Networking</p>
                      <Link href="/inventory/networks">
                        <Button
                          variant={pathname === "/inventory/networks" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <Network className="h-4 w-4 mr-2" />
                          Networks
                        </Button>
                      </Link>
                      
                      <Link href="/inventory/network-acls">
                        <Button
                          variant={pathname === "/inventory/network-acls" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <Shield className="h-4 w-4 mr-2" />
                          ACLs
                        </Button>
                      </Link>
                      
                      <Link href="/inventory/ipam">
                        <Button
                          variant={pathname === "/inventory/ipam" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <Network className="h-4 w-4 mr-2" />
                          IPAM
                        </Button>
                      </Link>
                    </div>
                    
                    {/* Storage Submenu */}
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Storage</p>
                      <Link href="/inventory/storage-pools">
                        <Button
                          variant={pathname === "/inventory/storage-pools" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <Database className="h-4 w-4 mr-2" />
                          Pools
                        </Button>
                      </Link>
                      
                      <Link href="/inventory/storage-volumes">
                        <Button
                          variant={pathname === "/inventory/storage-volumes" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <HardDrive className="h-4 w-4 mr-2" />
                          Volumes
                        </Button>
                      </Link>
                    </div>
                    
                    <Link href="/inventory/images">
                      <Button
                        variant={pathname === "/inventory/images" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                      >
                        <ImageIcon className="h-4 w-4 mr-2" />
                        Images
                      </Button>
                    </Link>
                    
                    <Link href="/inventory/server">
                      <Button
                        variant={pathname === "/inventory/server" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                      >
                        <Server className="h-4 w-4 mr-2" />
                        Server
                      </Button>
                    </Link>
                    
                    <Link href="/inventory/operations">
                      <Button
                        variant={pathname === "/inventory/operations" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                      >
                        <Activity className="h-4 w-4 mr-2" />
                        Operations
                      </Button>
                    </Link>
                    
                    <Link href="/inventory/warnings">
                      <Button
                        variant={pathname === "/inventory/warnings" ? "secondary" : "ghost"}
                        className="w-full justify-start text-sm"
                        size="sm"
                      >
                        <AlertTriangle className="h-4 w-4 mr-2" />
                        Warnings
                      </Button>
                    </Link>
                    
                    {/* Permissions Submenu */}
                    <div className="pl-2">
                      <p className="text-xs font-semibold text-muted-foreground mb-1 mt-2">Permissions</p>
                      <Link href="/inventory/identities">
                        <Button
                          variant={pathname === "/inventory/identities" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <UserCircle className="h-4 w-4 mr-2" />
                          Identities
                        </Button>
                      </Link>
                      
                      <Link href="/inventory/groups">
                        <Button
                          variant={pathname === "/inventory/groups" ? "secondary" : "ghost"}
                          className="w-full justify-start text-sm"
                          size="sm"
                        >
                          <Users className="h-4 w-4 mr-2" />
                          Groups
                        </Button>
                      </Link>
                    </div>
                  </div>
                )}
              </div>

              {/* Services Menu */}
              <div>
                <Link href="/service-providers">
                  <Button
                    variant={pathname === "/service-providers" ? "default" : "ghost"}
                    className={cn(
                      "w-full justify-start",
                      isCollapsed && "justify-center px-2"
                    )}
                  >
                    <Layers className="h-5 w-5" />
                    {!isCollapsed && (
                      <>
                        <span className="ml-3 flex-1 text-left">Services</span>
                        <Badge variant="secondary" className="ml-2">
                          1
                        </Badge>
                      </>
                    )}
                  </Button>
                </Link>
              </div>

              {/* Solutions Menu */}
              <div>
                <Link href="/solutions">
                  <Button
                    variant={pathname === "/solutions" ? "default" : "ghost"}
                    className={cn(
                      "w-full justify-start",
                      isCollapsed && "justify-center px-2"
                    )}
                  >
                    <HardDrive className="h-5 w-5" />
                    {!isCollapsed && (
                      <>
                        <span className="ml-3 flex-1 text-left">Solutions</span>
                        <Badge variant="secondary" className="ml-2">
                          2
                        </Badge>
                      </>
                    )}
                  </Button>
                </Link>
              </div>

              {/* Technologies Menu */}
              <div>
                <Link href="/technologies">
                  <Button
                    variant={pathname === "/technologies" ? "default" : "ghost"}
                    className={cn(
                      "w-full justify-start",
                      isCollapsed && "justify-center px-2"
                    )}
                  >
                    <Server className="h-5 w-5" />
                    {!isCollapsed && (
                      <span className="ml-3 flex-1 text-left">Technologies</span>
                    )}
                  </Button>
                </Link>
              </div>
            </div>
          </ScrollArea>

          {/* Footer with System Preferences */}
          <div className="border-t p-3">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  className={cn(
                    "w-full justify-start",
                    isCollapsed && "justify-center px-2"
                  )}
                >
                  <Settings className="h-5 w-5" />
                  {!isCollapsed && (
                    <span className="ml-3">System Preferences</span>
                  )}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
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
