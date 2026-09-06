"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert, Loader2 } from "lucide-react";
import { api, type BreakGlassGrant } from "@/lib/api";
import { toast } from "@/hooks/use-toast";
import { usePermissions } from "@/hooks/use-permissions";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { TableEmptyValue } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";

function StatusBadge({ status }: { status: string }) {
  if (status === "active") return <Badge className="bg-emerald-500/15 text-emerald-600 hover:bg-emerald-500/15">active</Badge>;
  if (status === "revoked") return <Badge variant="destructive">revoked</Badge>;
  return <Badge variant="secondary">{status}</Badge>;
}

/**
 * Break-glass governance view. Admins see all grants and can revoke an active one early;
 * partners/tenants see grants on their own organization's clusters (accountability).
 */
export function BreakGlassPanel() {
  const { isAdmin } = usePermissions();
  const [revoking, setRevoking] = useState<number | null>(null);
  const { data: grants = [], isLoading, refetch } = useQuery<BreakGlassGrant[]>({
    queryKey: ["break-glass-grants"],
    queryFn: () => api.breakGlass.listGrants(),
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
      await refetch();
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
          <ShieldAlert className="h-5 w-5 text-amber-500" />
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
                  <TableCell className="font-medium">{g.cluster_id}</TableCell>
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
