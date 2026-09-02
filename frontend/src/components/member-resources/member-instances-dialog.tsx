"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Activity, Layers } from "lucide-react";
import { formatInstanceType } from "@/lib/utils";
import type { MemberResources, VMInfo } from "@/types/api";

interface MemberInstancesDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  memberName: string | null;
  filter: "running" | "stopped" | null;
  instances: VMInfo[];
  isLoading: boolean;
  error: string | null;
  resources: MemberResources | null;
}

/**
 * The instance drill-down behind the Running and Stopped counts.
 *
 * important: it filters the tenant-scoped /instances list rather than the member scan that
 * produced the count. That scan applies no tenant ACL, so listing its names here would leak
 * across organizations. The list can therefore be shorter than the count, and the copy says
 * "visible to you" rather than pretending otherwise. Do not "fix" the mismatch by filtering
 * the count.
 */
export function MemberInstancesDialog({
  isOpen,
  onOpenChange,
  memberName,
  filter,
  instances,
  isLoading,
  error,
  resources,
}: MemberInstancesDialogProps) {
  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>
            {filter === "running" ? "Running" : "Stopped"} Instances on {memberName}
          </DialogTitle>
          <DialogDescription>
            {`${instances.length} ${filter} instance${instances.length === 1 ? "" : "s"} visible to you`}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="pr-2">
        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <Activity className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        ) : instances.length > 0 ? (
          <div className="space-y-3">
            {instances.map((instance, index) => (
              <Card key={index}>
                <CardContent className="pt-6">
                  <div className="flex items-start justify-between">
                    <div className="space-y-1 flex-1">
                      <div className="flex items-center gap-2">
                        <h4 className="font-semibold">{instance.name}</h4>
                        <Badge variant={instance.status === "Running" ? "default" : "secondary"} className="text-xs">
                          {instance.status}
                        </Badge>
                        <span className="text-xs text-muted-foreground">
                          {formatInstanceType(instance.type)}
                        </span>
                      </div>
                      <div className="text-sm text-muted-foreground space-y-1">
                        {instance.ip_addresses && instance.ip_addresses.length > 0 && (
                          <div className="flex items-center gap-2">
                            <span className="font-medium">IP:</span>
                            <span className="font-mono text-xs">{instance.ip_addresses.join(", ")}</span>
                          </div>
                        )}
                        <div className="flex items-center gap-4">
                          <span>
                            <span className="font-medium">Type:</span> {formatInstanceType(instance.type)}
                          </span>
                          <span>
                            <span className="font-medium">Project:</span> {instance.project || "default"}
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        ) : error ? (
          <EmptyState
            icon={Layers}
            title="Could not load instances"
            description={error}
          />
        ) : (
          <EmptyState
            icon={Layers}
            title={`No ${filter} instances visible to you`}
            description={
              `The member reports ${
                filter === "running"
                  ? resources?.instances_running ?? 0
                  : (resources?.instances_count || 0) - (resources?.instances_running || 0)
              } ${filter}. Any not listed here belong to an organization you cannot see.`
            }
          />
        )}
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
