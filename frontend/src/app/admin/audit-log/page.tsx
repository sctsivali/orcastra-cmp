"use client";

/**
 * Audit Log viewer (mini deployment)
 *
 * Reads the Postgres hash-chained audit log that replaces OpenSearch when centralized
 * logging is absent. Filtering, date-range, sorting, and integrity check are all
 * server-side (the table is paginated). Admin-only.
 */

import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { RefreshButton } from "@/components/ui/refresh-button";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useToast } from "@/hooks/use-toast";
import { auditLogAPI, type AuditEntry } from "@/lib/api";
import type { SortDirection } from "@/lib/sort";
import { ShieldCheck, Search, Download } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";

const PAGE_SIZE = 50;

export default function AuditLogPage() {
  const { toast } = useToast();
  const [page, setPage] = useState(0);
  const [actionFilter, setActionFilter] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [sortBy, setSortBy] = useState("occurred_at");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");

  // Server-side sort (the table is paginated, so client-side would only sort one page).
  const onSort = (field: string) => {
    if (field === sortBy) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortBy(field);
      setSortDir("asc");
    }
    setPage(0);
  };

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["audit-log", page, actionFilter, actorFilter, startDate, endDate, sortBy, sortDir],
    queryFn: () =>
      auditLogAPI.list({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        action: actionFilter || undefined,
        actor: actorFilter || undefined,
        start: startDate || undefined,
        end: endDate || undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
      }),
  });

  const verifyMutation = useMutation({
    mutationFn: auditLogAPI.verify,
    onSuccess: (r) =>
      toast({
        title: r.valid ? "Audit chain intact" : "Audit chain BROKEN",
        description: r.valid
          ? `Verified ${r.checked} entries.`
          : `Tampering detected at entry id ${r.broken_at_id} (checked ${r.checked}).`,
        variant: r.valid ? "default" : "destructive",
      }),
    onError: (e: any) =>
      toast({ title: "Verify failed", description: e.message, variant: "destructive" }),
  });

  const [isExporting, setIsExporting] = useState(false);
  const handleExport = async (format: "csv" | "json") => {
    setIsExporting(true);
    try {
      const { blob, filename } = await auditLogAPI.export(format, {
        action: actionFilter || undefined,
        actor: actorFilter || undefined,
        start: startDate || undefined,
        end: endDate || undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
      });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
      toast({ title: "Export ready", description: filename });
    } catch (e: any) {
      toast({ title: "Export failed", description: e.message, variant: "destructive" });
    } finally {
      setIsExporting(false);
    }
  };

  const items: AuditEntry[] = data?.items || [];
  const total = data?.total || 0;

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">Audit Log</h1>
          <p className="text-muted-foreground mt-2">
            Tamper-evident audit trail stored in PostgreSQL.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton onClick={() => refetch()} isRefreshing={isFetching} />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" disabled={isExporting}>
                <Download className="mr-2 h-4 w-4" />
                {isExporting ? "Exporting..." : "Export"}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={() => handleExport("csv")}>Export as CSV</DropdownMenuItem>
              <DropdownMenuItem onClick={() => handleExport("json")}>Export as JSON</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button onClick={() => verifyMutation.mutate()} disabled={verifyMutation.isPending}>
            <ShieldCheck className="mr-2 h-4 w-4" />
            {verifyMutation.isPending ? "Verifying..." : "Verify integrity"}
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Events ({total})</CardTitle>
          <div className="flex flex-wrap items-end gap-3 pt-2">
            <div className="relative">
              <Search className="absolute left-2 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                className="pl-8 w-56"
                placeholder="Filter by action"
                value={actionFilter}
                onChange={(e) => { setPage(0); setActionFilter(e.target.value); }}
              />
            </div>
            <Input
              className="w-56"
              placeholder="Filter by actor"
              value={actorFilter}
              onChange={(e) => { setPage(0); setActorFilter(e.target.value); }}
            />
            <div className="flex flex-col gap-1">
              <Label htmlFor="audit-from" className="text-xs text-muted-foreground">From</Label>
              <Input
                id="audit-from" type="date" className="w-40"
                value={startDate}
                onChange={(e) => { setPage(0); setStartDate(e.target.value); }}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="audit-to" className="text-xs text-muted-foreground">To</Label>
              <Input
                id="audit-to" type="date" className="w-40"
                value={endDate}
                onChange={(e) => { setPage(0); setEndDate(e.target.value); }}
              />
            </div>
            {(startDate || endDate) && (
              <Button
                variant="ghost" size="sm"
                onClick={() => { setPage(0); setStartDate(""); setEndDate(""); }}
              >
                Clear dates
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : items.length === 0 ? (
            <EmptyState icon={ShieldCheck} title="No audit events" />
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Time" field="occurred_at" currentField={sortBy} direction={sortDir} onSort={onSort} />
                    <SortableTableHead label="Actor" field="actor_id" currentField={sortBy} direction={sortDir} onSort={onSort} />
                    <SortableTableHead label="Action" field="action" currentField={sortBy} direction={sortDir} onSort={onSort} />
                    <SortableTableHead label="Target" field="target_type" currentField={sortBy} direction={sortDir} onSort={onSort} />
                    <SortableTableHead label="Result" field="result" currentField={sortBy} direction={sortDir} onSort={onSort} />
                    <SortableTableHead label="Source IP" field="source_ip" currentField={sortBy} direction={sortDir} onSort={onSort} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((e) => (
                    <TableRow key={e.id}>
                      <TableCell className="text-sm whitespace-nowrap">
                        {new Date(e.occurred_at).toLocaleString()}
                      </TableCell>
                      <TableCell className="text-sm">
                        <div>{e.actor_email || e.actor_id || "-"}</div>
                        {e.actor_role && <div className="text-xs text-muted-foreground">{e.actor_role}</div>}
                      </TableCell>
                      <TableCell><code className="text-xs">{e.action}</code></TableCell>
                      <TableCell className="text-sm">
                        {e.target_type ? `${e.target_type}/${e.target_id ?? ""}` : "-"}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{e.result || "success"}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{e.source_ip || "-"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <div className="pt-4">
                <DataTablePagination
                  currentPage={page + 1}
                  totalPages={Math.max(1, Math.ceil(total / PAGE_SIZE))}
                  onPageChange={(p: number) => setPage(p - 1)}
                  totalItems={total}
                  pageSize={PAGE_SIZE}
                  onPageSizeChange={() => { /* fixed page size */ }}
                />
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
