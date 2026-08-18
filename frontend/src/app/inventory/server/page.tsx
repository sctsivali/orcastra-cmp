"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
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
import { ServerInfo } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { useServerInfo } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";

function ConfigItemsBadge({ config }: { config: Record<string, any> }) {
  const [open, setOpen] = useState(false);
  const itemCount = Object.keys(config).length;

  if (itemCount === 0) {
    return <Badge variant="outline">0 items</Badge>;
  }

  return (
    <>
      <Badge
        variant="secondary"
        className="cursor-pointer hover:bg-secondary/80 transition-colors"
        onClick={() => setOpen(true)}
      >
        {itemCount} items
      </Badge>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-4xl max-h-[85vh] flex flex-col overflow-hidden">
          <DialogHeader className="pr-8 flex-shrink-0">
            <DialogTitle className="text-xl">
              Configuration Items
            </DialogTitle>
            <DialogDescription>
              {itemCount} configuration item{itemCount !== 1 ? 's' : ''}
            </DialogDescription>
          </DialogHeader>

          <div className="flex-1 min-h-0 overflow-y-auto pr-2">
            <div className="space-y-3">
              {Object.entries(config).map(([key, value], index) => (
                <div key={index} className="border rounded-lg p-4 space-y-2 hover:bg-muted/30 transition-colors">
                  <div className="flex items-start gap-3">
                    <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-primary/10 text-primary font-semibold text-sm flex-shrink-0">
                      {index + 1}
                    </div>
                    <div className="flex-1 min-w-0 space-y-2">
                      <div>
                        <div className="text-xs text-muted-foreground mb-1">Configuration Key</div>
                        <div className="font-semibold text-sm font-mono break-all bg-muted/50 p-2 rounded">
                          {key}
                        </div>
                      </div>
                      <div>
                        <div className="text-xs text-muted-foreground mb-1">Value</div>
                        {typeof value === 'object' ? (
                          <pre className="text-xs bg-muted/70 p-3 rounded overflow-x-auto font-mono">
                            {JSON.stringify(value, null, 2)}
                          </pre>
                        ) : (
                          <div className="font-medium text-sm font-mono bg-muted/50 p-2 rounded break-all">
                            {String(value)}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default function ServerPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);
  const { data, isLoading, refetch, isRefetching } = useServerInfo();
  const servers = (data as any)?.servers || [];
  const { toast } = useToast();

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Server Information
          </h1>
          <p className="text-muted-foreground mt-2">
            View hypervisor server details and configuration
          </p>
        </div>
        <RefreshButton
          disabled={isRefetching}
          isRefreshing={isRefetching}
          label="Refresh server information"
          onClick={() => refetch()}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {servers.map((server: any) => (
          <Card key={server.node}>
            <CardHeader>
              <CardTitle>
                {server.node}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Hypervisor API Information
                </div>
                <div className="space-y-2">
                  <div className="flex justify-between">
                    <span className="text-sm">Hypervisor API Version:</span>
                    <Badge variant="outline">{server.node_type} {server.api_version}</Badge>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Authentication:</span>
                    <Badge variant="secondary">{server.auth}</Badge>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Public:</span>
                    <Badge variant={server.public ? "default" : "outline"}>
                      {server.public ? "Yes" : "No"}
                    </Badge>
                  </div>
                </div>
              </div>

              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Environment
                </div>
                <div className="space-y-1 max-h-32 overflow-y-auto">
                  {Object.entries(server.environment)
                    .filter(([key]) => !key.includes('certificate'))
                    .slice(0, 10)
                    .map(([key, value]) => (
                      <div key={key} className="text-xs break-all">
                        <span className="font-mono text-muted-foreground">{key}:</span>{" "}
                        <span className="font-mono break-words">
                          {typeof value === 'object' ? JSON.stringify(value).slice(0, 50) + '...' : String(value).slice(0, 80)}
                        </span>
                      </div>
                    ))}
                </div>
              </div>

              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Configuration
                </div>
                <ConfigItemsBadge config={server.config} />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
