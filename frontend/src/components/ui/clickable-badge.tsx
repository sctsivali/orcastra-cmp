"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import {
  Dialog,
  DialogBody,
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
function getDeviceColor(_type: string) {
  return "bg-foreground text-background";
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
    return <TableEmptyValue label={`No ${label}`} />;
  }

  // Check if items are device objects (have name, type, config properties)
  const isDeviceList = label === "devices" && items.length > 0 && 
    typeof items[0] === "object" && 
    "name" in items[0] && 
    "type" in items[0];

  return (
    <>
      <TableLink onClick={() => setOpen(true)} aria-label={`Show ${count} ${label} for ${title}`}>
        {count} {label}
      </TableLink>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-4xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
          <DialogHeader className="pr-8">
            <DialogTitle className="text-xl">{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>

          <DialogBody className="pr-4">
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
                        {formatLabel(device.type)}
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
          </DialogBody>
        </DialogContent>
      </Dialog>
    </>
  );
}
