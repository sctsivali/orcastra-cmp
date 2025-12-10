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

interface ClickableBadgeProps {
  count: number;
  label: string;
  items: string[] | Record<string, any>[];
  title: string;
  description?: string;
  renderItem?: (item: any, index: number) => React.ReactNode;
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
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>

          <div className="mt-4">
            {items.length === 0 ? (
              <p className="text-muted-foreground text-center py-4">No items found</p>
            ) : typeof items[0] === "string" ? (
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
              <div className="space-y-2">
                {items.map((item, index) => (
                  <div key={index}>
                    {renderItem ? renderItem(item, index) : (
                      <pre className="text-xs bg-muted p-2 rounded">
                        {JSON.stringify(item, null, 2)}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
