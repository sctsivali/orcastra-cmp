"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { EmptyState } from "@/components/ui/empty-state";
import { Server, ServerCrash } from "lucide-react";
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
import { useServerInfo } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { usePermissions } from "@/hooks/use-permissions";
import { ServerHardware } from "@/components/server/server-hardware";

function ConfigItemsBadge({ config }: { config: Record<string, any> }) {
  const [open, setOpen] = useState(false);
  const itemCount = Object.keys(config).length;

  if (itemCount === 0) {
    return <span className="text-muted-foreground">0 items</span>;
  }

  return (
    <>
      <button
        type="button"
        className="cursor-pointer text-foreground hover:underline focus:outline-none focus-visible:underline"
        onClick={() => setOpen(true)}
      >
        {itemCount} items
      </button>

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
  const router = useRouter();
  const { isPartner } = usePermissions();
  const { data, isLoading, isError, refetch, isRefetching } = useServerInfo();
  const servers = (data as any)?.servers || [];
  const autoRetriedRef = useRef(false);

  // redirect if not Partner+
  useEffect(() => {
    if (!isPartner) {
      router.replace("/");
    }
  }, [isPartner, router]);

  // One-shot auto-retry: a cold-start cluster connection can momentarily return an
  // empty list before the hypervisor link is warm. Retry once so the page recovers
  // without the user having to hit Refresh, but never loop when there are truly none.
  useEffect(() => {
    if (isLoading || isRefetching || autoRetriedRef.current) return;
    if (!isError && servers.length === 0) {
      autoRetriedRef.current = true;
      refetch();
    }
  }, [isLoading, isRefetching, isError, servers.length, refetch]);

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

      {isError ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={ServerCrash}
              title="Could not load server information"
              description="The hypervisor may be unreachable."
              action={
                <RefreshButton
                  disabled={isRefetching}
                  isRefreshing={isRefetching}
                  label="Retry"
                  onClick={() => refetch()}
                />
              }
            />
          </CardContent>
        </Card>
      ) : servers.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={Server}
              title="No hypervisor connected yet"
              description="If a cluster was just registered, the connection may still be establishing."
              action={
                <RefreshButton
                  disabled={isRefetching}
                  isRefreshing={isRefetching}
                  label="Retry"
                  onClick={() => refetch()}
                />
              }
            />
          </CardContent>
        </Card>
      ) : (
      <div className="grid grid-cols-1 gap-6">
        {servers.map((server: any) => (
          <Card key={server.node}>
            <CardHeader>
              <CardTitle>
                {server.node}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                <div>
                  <div className="text-sm font-medium text-muted-foreground mb-2">
                    Hypervisor API Information
                  </div>
                  <div className="space-y-2">
                    <div className="flex justify-between">
                      <span className="text-sm">Hypervisor API Version:</span>
                      <span className="text-sm text-right">{server.node_type} {server.api_version}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-sm">Authentication:</span>
                      <span className="text-sm text-right">{server.auth}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-sm">Public:</span>
                      <span className="text-sm text-right">{server.public ? "Yes" : "No"}</span>
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
              </div>

              <div className="border-t pt-6">
                <div className="text-base font-semibold mb-4">Hardware</div>
                <ServerHardware node={server.node} />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
      )}
    </div>
  );
}
