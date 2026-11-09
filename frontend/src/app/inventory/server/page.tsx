"use client";

import { useMemo, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { RefreshButton } from "@/components/ui/refresh-button";
import { UnreachableNodesBanner, reachabilityOf } from "@/components/ui/unreachable-nodes-banner";
import { EmptyState } from "@/components/ui/empty-state";
import { DisconnectedIllustration } from "@/components/illustrations/scenes/states";
import { Server } from "lucide-react";
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
import { GRID_PAGE_SIZE, GRID_PAGE_SIZES } from "@/lib/pagination";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useRequireRole } from "@/hooks/use-require-role";
import { Role } from "@/types/rbac";
import { formatHypervisorType, plural, singular } from "@/lib/utils";

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
      <TableLink onClick={() => setOpen(true)} aria-label={`Show ${plural(itemCount, singular(noun), noun)} in ${title}`}>
        {plural(itemCount, singular(noun), noun)}
      </TableLink>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-4xl max-h-[85vh] flex flex-col overflow-hidden">
          <DialogHeader className="pr-8 flex-shrink-0 pb-4">
            <DialogTitle className="text-xl">{title}</DialogTitle>
            <DialogDescription>
              {plural(itemCount, singular(noun), noun)}
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
  const { data, isLoading, isError, error, refetch, refreshNow, dataUpdatedAt } = useServerInfo(undefined, isResolved && isPartner);
  const servers: any[] = useMemo(() => (data as any)?.servers || [], [data]);
  // Clusters that never answered: their server details are absent from the grid, so warn
  // rather than showing fewer cards with nothing to explain it.
  const reach = reachabilityOf(data);
  const { toast } = useToast();

  // The grid is one, two or three columns wide, so it takes the grid page sizes rather than the
  // table set: every one of them divides by all three and a full page never ends in a ragged row.
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(GRID_PAGE_SIZE);

  // note: clamped on read rather than corrected in an effect. An effect would paint one wrong
  // frame first and then snap the operator back a page.
  const totalPages = Math.max(1, Math.ceil(servers.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedServers = servers.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (!isResolved) return <LoadingLottiePage />;
  if (!isPartner) return null;

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Server Information
          </h1>
          <p className="text-muted-foreground mt-2">
            The hypervisor API, environment and server configuration reported by each cluster
          </p>
        </div>
        <RefreshButton
          label="Refresh server information"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner
        noun="server details"
        nodes={reach.unreachable}
        partialNodes={reach.partial}
        lateNodes={reach.late}
        reasons={reach.reasons}
      />

      {isError ? (
        // a request that failed is not a fleet with nothing: say which, so "No cluster
        // answered" is only ever reached on a successful response with zero rows
        <Card>
          <CardContent>
            <EmptyState
              icon={Server}
              illustration={DisconnectedIllustration}
              title="Could not load server details"
              description={error instanceof Error ? error.message : "The request failed. Use Refresh to try again."}
            />
          </CardContent>
        </Card>
      ) : servers.length === 0 ? (
        // note: an empty grid said nothing at all. With every cluster named in a banner above,
        // this tells the operator the page itself is fine and the fleet is what it is.
        <Card>
          <CardContent>
            <EmptyState
              icon={Server}
              title="No server details to show"
              description={
                reach.unreachable.length || reach.partial.length
                  ? "The clusters named above could not be read. Their details will appear once they answer."
                  : "No cluster answered with server details."
              }
            />
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {pagedServers.map((server: any) => (
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
                    <span className="text-sm">API Version:</span>
                    <Badge variant="outline">{formatHypervisorType(server.node_type)} {server.api_version}</Badge>
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

      {/* Absent rather than disabled when every node fits on one page. */}
      {totalPages > 1 && (
        <DataTablePagination
          currentPage={currentPage}
          totalPages={totalPages}
          pageSize={pageSize}
          pageSizeOptions={[...GRID_PAGE_SIZES]}
          totalItems={servers.length}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}
