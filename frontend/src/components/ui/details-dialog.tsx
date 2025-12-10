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

export function DevicesDialog({ open, onOpenChange, devices }: DevicesDialogProps) {
  const deviceEntries = Object.entries(devices);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Devices Configuration</DialogTitle>
          <DialogDescription>
            {deviceEntries.length} device{deviceEntries.length !== 1 ? "s" : ""} configured
          </DialogDescription>
        </DialogHeader>
        <ScrollArea className="max-h-[500px] pr-4">
          <div className="space-y-4">
            {deviceEntries.length === 0 ? (
              <p className="text-muted-foreground text-center py-8">No devices configured</p>
            ) : (
              deviceEntries.map(([deviceName, deviceConfig]) => (
                <div key={deviceName} className="border rounded-lg p-4 space-y-2">
                  <div className="flex items-center gap-2 mb-2">
                    <Badge variant="default">{deviceName}</Badge>
                    <Badge variant="outline">{deviceConfig.type || "unknown"}</Badge>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    {Object.entries(deviceConfig).map(([key, value]) => (
                      <div key={key} className="flex flex-col">
                        <span className="text-muted-foreground font-mono text-xs">{key}</span>
                        <span className="font-medium truncate" title={value}>{value}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
