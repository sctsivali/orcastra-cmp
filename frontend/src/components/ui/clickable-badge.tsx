"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ScrollArea } from "@/components/ui/scroll-area";
import { 
  HardDrive, 
  Network, 
  Cpu, 
  Sparkles, 
  Box, 
  Usb,
  Monitor,
  Disc,
  Cable
} from "lucide-react";

interface ClickableBadgeProps {
  count: number;
  label: string;
  items: string[] | Record<string, any>[];
  title: string;
  description?: string;
  renderItem?: (item: any, index: number) => React.ReactNode;
}

// Helper function to get device type icon
function getDeviceIcon(type: string) {
  const iconMap: Record<string, any> = {
    disk: HardDrive,
    nic: Network,
    gpu: Sparkles,
    usb: Usb,
    unix: Cable,
    proxy: Cable,
    infiniband: Network,
    tpm: Box,
    pci: Cpu,
    none: Box,
  };
  
  const IconComponent = iconMap[type?.toLowerCase()] || Box;
  return <IconComponent className="h-4 w-4" />;
}

// Helper function to get device type color
function getDeviceColor(type: string) {
  const colorMap: Record<string, string> = {
    disk: "bg-blue-100 text-blue-600 dark:bg-blue-950 dark:text-blue-400",
    nic: "bg-green-100 text-green-600 dark:bg-green-950 dark:text-green-400",
    gpu: "bg-purple-100 text-purple-600 dark:bg-purple-950 dark:text-purple-400",
    usb: "bg-yellow-100 text-yellow-600 dark:bg-yellow-950 dark:text-yellow-400",
    unix: "bg-orange-100 text-orange-600 dark:bg-orange-950 dark:text-orange-400",
    proxy: "bg-pink-100 text-pink-600 dark:bg-pink-950 dark:text-pink-400",
    infiniband: "bg-cyan-100 text-cyan-600 dark:bg-cyan-950 dark:text-cyan-400",
    tpm: "bg-indigo-100 text-indigo-600 dark:bg-indigo-950 dark:text-indigo-400",
    pci: "bg-red-100 text-red-600 dark:bg-red-950 dark:text-red-400",
  };
  
  return colorMap[type?.toLowerCase()] || "bg-gray-100 text-gray-600 dark:bg-gray-950 dark:text-gray-400";
}

export function ClickableBadge({
  count,
  label,
  items,
  title,
  description,
  renderItem,
}: ClickableBadgeProps) {
  const [open, setOpen] = useState(false);

  if (count === 0) {
    return <Badge variant="outline" className="flex items-center justify-center gap-1">0 {label}</Badge>;
  }

  // Check if items are device objects (have name, type, config properties)
  const isDeviceList = label === "devices" && items.length > 0 && 
    typeof items[0] === "object" && 
    "name" in items[0] && 
    "type" in items[0];

  return (
    <>
      <Badge
        variant="secondary"
        className="cursor-pointer hover:bg-secondary/80 transition-colors flex items-center justify-center gap-1"
        onClick={() => setOpen(true)}
      >
        {count} {label}
      </Badge>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-4xl max-h-[85vh]">
          <DialogHeader className="pr-8">
            <DialogTitle className="text-xl">{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>

          <ScrollArea className="max-h-[calc(85vh-8rem)] pr-4">
            <div className="space-y-3">
              {items.length === 0 ? (
                <p className="text-muted-foreground text-center py-8">No items found</p>
              ) : isDeviceList ? (
                // Enhanced device rendering
                (items as any[]).map((device, index) => (
                  <div 
                    key={index} 
                    className="border rounded-lg p-4 space-y-3 hover:bg-muted/30 transition-colors"
                  >
                    {/* Device Header */}
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <div className={`flex h-10 w-10 items-center justify-center rounded-lg ${getDeviceColor(device.type)}`}>
                          {getDeviceIcon(device.type)}
                        </div>
                        <div>
                          <div className="font-semibold text-base">{device.name}</div>
                          <div className="text-xs text-muted-foreground">Device Name</div>
                        </div>
                      </div>
                      <Badge variant="outline" className="text-sm">
                        {device.type || "N/A"}
                      </Badge>
                    </div>

                    {/* Device Configuration */}
                    {device.config && Object.keys(device.config).length > 0 && (
                      <div className="space-y-2 pt-3 border-t">
                        <div className="text-sm font-semibold text-muted-foreground mb-2">Configuration</div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                          {Object.entries(device.config).map(([key, value]: [string, any]) => (
                            <div key={key} className="flex flex-col gap-1 p-2 rounded-md bg-muted/50">
                              <span className="text-xs font-medium text-muted-foreground font-mono">
                                {key}
                              </span>
                              <span className="text-sm font-medium truncate" title={String(value)}>
                                {String(value)}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ))
              ) : typeof items[0] === "string" ? (
                // String items (ex; used_by resources)
                <div className="space-y-2">
                  {(items as string[]).map((item, index) => {
                    const match = item.match(/\/1\.0\/(\w+)\/([^/?]+)/);
                    const resourceType = match ? match[1] : 'resource';
                    const resourceName = match ? match[2] : item;
                    
                    return (
                      <div key={index} className="flex items-center gap-3 p-3 rounded-lg border bg-card hover:bg-muted/50 transition-colors">
                        <div className="flex items-center justify-center w-8 h-8 rounded-full bg-primary/10 text-primary font-semibold text-sm">
                          {index + 1}
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="font-medium text-sm truncate">{resourceName}</div>
                          <div className="text-xs text-muted-foreground">{resourceType}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                // Custom rendered items
                <div className="space-y-2">
                  {items.map((item, index) => (
                    <div key={index}>
                      {renderItem ? renderItem(item, index) : (
                        <pre className="text-xs bg-muted p-2 rounded overflow-x-auto">
                          {JSON.stringify(item, null, 2)}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  );
}
