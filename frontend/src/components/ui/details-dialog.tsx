"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { 
  HardDrive, 
  Network, 
  Cpu, 
  Sparkles, 
  Box, 
  Usb,
  Cable
} from "lucide-react";

interface UsedByDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  items: string[];
}

export function UsedByDialog({ open, onOpenChange, title, items }: UsedByDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {items.length} item{items.length !== 1 ? "s" : ""} using this resource
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[400px] pr-4">
          <div className="space-y-2">
            {items.length === 0 ? (
              <p className="text-muted-foreground text-center py-8">No items found</p>
            ) : (
              items.map((item, index) => (
                <div key={index} className="flex items-center gap-2 p-2 hover:bg-accent rounded-md">
                  <Badge variant="outline" className="font-mono text-xs">
                    {item}
                  </Badge>
                </div>
              ))
            )}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

interface DevicesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  devices: Record<string, Record<string, string>>;
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

export function DevicesDialog({ open, onOpenChange, devices }: DevicesDialogProps) {
  const deviceEntries = Object.entries(devices);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[85dvh] max-h-[85vh] overscroll-contain">
        <DialogHeader>
          <DialogTitle className="text-xl">Devices Configuration</DialogTitle>
          <DialogDescription>
            {deviceEntries.length} device{deviceEntries.length !== 1 ? "s" : ""} configured
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[calc(85dvh-8rem)] max-h-[calc(85vh-8rem)] pr-4">
          <div className="space-y-3">
            {deviceEntries.length === 0 ? (
              <p className="text-muted-foreground text-center py-8">No devices configured</p>
            ) : (
              deviceEntries.map(([deviceName, deviceConfig]) => (
                <div key={deviceName} className="border rounded-lg p-4 space-y-3 hover:bg-muted/30 transition-colors">
                  {/* Device Header */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className={`flex h-10 w-10 items-center justify-center rounded-lg ${getDeviceColor(deviceConfig.type)}`}>
                        {getDeviceIcon(deviceConfig.type)}
                      </div>
                      <div>
                        <div className="font-semibold text-base">{deviceName}</div>
                        <div className="text-xs text-muted-foreground">Device Name</div>
                      </div>
                    </div>
                    <Badge variant="outline" className="text-sm">
                      {deviceConfig.type || "unknown"}
                    </Badge>
                  </div>

                  {/* Device Configuration */}
                  {Object.keys(deviceConfig).length > 0 && (
                    <div className="space-y-2 pt-3 border-t">
                      <div className="text-sm font-semibold text-muted-foreground mb-2">Configuration</div>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {Object.entries(deviceConfig).map(([key, value]) => (
                          <div key={key} className="flex flex-col gap-1 p-2 rounded-md bg-muted/50">
                            <span className="text-xs font-medium text-muted-foreground font-mono">
                              {key}
                            </span>
                            <span className="text-sm font-medium truncate" title={value}>
                              {value}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
