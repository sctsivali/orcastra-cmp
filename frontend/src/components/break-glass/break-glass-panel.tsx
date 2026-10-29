"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert, Loader2 } from "lucide-react";
import { api, type BreakGlassGrant, type BreakGlassGrantStatus } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { usePermissions } from "@/hooks/use-permissions";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { TableEmptyValue } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";
import { NodeLink } from "@/components/ui/node-link";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";

const BREAK_GLASS_STATUS: Record<BreakGlassGrantStatus, { label: string; tone: BadgeTone }> = {
  active: { label: "Active", tone: "positive" },
  expired: { label: "Expired", tone: "neutral" },
  revoked: { label: "Revoked", tone: "negative" },
};

function StatusBadge({ status }: { status: BreakGlassGrantStatus }) {
  const meta = BREAK_GLASS_STATUS[status];
  return <Badge variant={meta.tone}>{meta.label}</Badge>;
}

/**
 * Break-glass governance view. Admins see all grants and can revoke an active one early;
 * partners/tenants see grants on their own organization's clusters (accountability).
 */
export function BreakGlassPanel() {
  const { isAdmin } = usePermissions();
  const { scopeKey, isAuthenticated } = useActorQueryScope();
  const queryClient = useQueryClient();
  const grantsQueryKey = ["break-glass-grants", scopeKey] as const;
  const [revoking, setRevoking] = useState<number | null>(null);
  const { data: grants = [], isLoading } = useQuery<BreakGlassGrant[]>({
    queryKey: grantsQueryKey,
    queryFn: () => api.breakGlass.listGrants(),
    enabled: isAuthenticated,
    refetchInterval: 15000,
  });

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  // note: clamped on read, never corrected in an effect. This list refetches every 15 seconds,
  // so an effect would paint one wrong frame and then move the page under whoever is reading it.
  const totalPages = Math.max(1, Math.ceil(grants.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedGrants = grants.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const revoke = async (id: number) => {
    setRevoking(id);
    try {
      await api.breakGlass.revokeGrant(id);
      // Update the actor that issued this revoke without another network read. The component can
      // survive a sign-in change while the request is in flight, so refetching here could send the
      // new session's response into this old callback's key. Retained actor caches are only marked
      // stale; their own observer or interval performs the next read under the matching session.
      queryClient.setQueryData<BreakGlassGrant[]>(grantsQueryKey, (current) =>
        current?.map((grant) =>
          grant.id === id ? { ...grant, status: "revoked" } : grant,
        ),
      );
      await reconcileMutationImpact(queryClient, "break-glass.write", {
        queryKeys: [grantsQueryKey],
        refetchType: "none",
      }).catch(() => {
        // The revoke already succeeded and the exact row above is authoritative. A failed
        // background refetch must not tell the operator that the grant is still active.
      });
      toast({
        title: "Grant revoked",
        description: "Access ended immediately. The grant stays on the audit trail.",
        variant: "success",
      });
    } catch (error) {
      toast({
        title: "Revoke failed",
        description:
          error instanceof Error ? error.message : "The grant is still active. Please try again.",
        variant: "destructive",
      });
    } finally {
      setRevoking(null);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="h-5 w-5 text-status-pending" aria-hidden="true" />
          Break-Glass Grants
        </CardTitle>
        <CardDescription>
          {isAdmin
            ? "Time-boxed, justified admin access to tenant console/terminal. Revoke to end access early."
            : "Operator access to your organization's clusters. Every grant is recorded here."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground py-6">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading grants...
          </div>
        ) : grants.length === 0 ? (
          <p className="text-sm text-muted-foreground py-6">No break-glass grants.</p>
        ) : (
          <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Cluster</TableHead>
                <TableHead>Organization</TableHead>
                <TableHead>Admin</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Expires</TableHead>
                {isAdmin ? <TableHead className="text-right">Action</TableHead> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {pagedGrants.map((g) => (
                <TableRow key={g.id}>
                  <TableCell className="font-medium"><NodeLink clusterId={g.cluster_id} /></TableCell>
                  <TableCell>{g.target_org_slug || <TableEmptyValue label="No target organization" />}</TableCell>
                  <TableCell className="text-muted-foreground">{g.admin_email}</TableCell>
                  <TableCell className="max-w-xs">
                    <OverflowText
                      value={g.reason}
                      label={`reason for the grant on ${g.cluster_id}`}
                      lines={2}
                    />
                  </TableCell>
                  <TableCell><StatusBadge status={g.status} /></TableCell>
                  <TableCell className="text-muted-foreground">{new Date(g.expires_at).toLocaleString()}</TableCell>
                  {isAdmin ? (
                    <TableCell className="text-right">
                      {g.status === "active" ? (
                        <Button size="sm" variant="outline" disabled={revoking === g.id} onClick={() => revoke(g.id)}>
                          {revoking === g.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Revoke"}
                        </Button>
                      ) : null}
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {totalPages > 1 && (
            <div className="mt-4">
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                pageSizeOptions={[...PANEL_PAGE_SIZES]}
                totalItems={grants.length}
                onPageChange={setPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setPage(1);
                }}
              />
            </div>
          )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
