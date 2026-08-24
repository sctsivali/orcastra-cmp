"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue } from "@/components/ui/table-text";
import { ServerInfo } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { useServerInfo } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";

// important: the certificate keys stay excluded. They were filtered out of the inline list on
// purpose, and moving the data into a dialog must not quietly widen what the page discloses.
function environmentWithoutCertificates(environment: Record<string, any>): Record<string, any> {
  return Object.fromEntries(
    Object.entries(environment ?? {}).filter(([key]) => !key.includes("certificate")),
  );
}

// note: one component for both key/value maps on this page. The Environment block used to
// render inline with .slice(0, 10) on the entries and .slice(0, 80) on each value, so an
// operator could neither see the eleventh key nor read a long one. Both maps now show a count
// that opens the whole thing untruncated.
function ConfigMapLink({
  data,
  title,
  noun,
  emptyLabel,
}: {
  data: Record<string, any>;
  title: string;
  noun: string;
  emptyLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const entries = Object.entries(data);
  const itemCount = entries.length;

  if (itemCount === 0) {
    return <TableEmptyValue label={emptyLabel} />;
  }

  return (
    <>
      <TableLink onClick={() => setOpen(true)} aria-label={`Show ${itemCount} ${noun} in ${title}`}>
        {itemCount} {noun}
      </TableLink>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-4xl max-h-[85vh] flex flex-col overflow-hidden">
          <DialogHeader className="pr-8 flex-shrink-0">
            <DialogTitle className="text-xl">{title}</DialogTitle>
            <DialogDescription>
              {itemCount} {noun}
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="pr-2">
            <div className="space-y-3">
              {entries.map(([key, value], index) => (
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
          </DialogBody>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default function ServerPage() {
  const { allowed: isPartner, isResolved } = useRequireRole(Role.PARTNER);
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useServerInfo();
  const servers = (data as any)?.servers || [];
  // Clusters that never answered: their server details are absent from the grid, so warn
  // rather than showing fewer cards with nothing to explain it.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
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
          label="Refresh server information"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="server details" nodes={unreachableNodes} />

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
                    <Badge variant="outline">
                      {server.public ? "Yes" : "No"}
                    </Badge>
                  </div>
                </div>
              </div>

              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Environment
                </div>
                <ConfigMapLink
                  data={environmentWithoutCertificates(server.environment)}
                  title="Server Environment"
                  noun="keys"
                  emptyLabel="No environment"
                />
              </div>

              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Configuration
                </div>
                <ConfigMapLink
                  data={server.config}
                  title="Server Configuration"
                  noun="items"
                  emptyLabel="No configuration"
                />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
