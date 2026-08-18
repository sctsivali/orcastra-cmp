"use client";

import { useMemo, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { RefreshButton } from "@/components/ui/refresh-button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { IsoUploadDialog } from "@/components/iso-upload-dialog";
import { useStorageVolumes } from "@/hooks/use-lxd";
import { useTableSort } from "@/hooks/use-table-sort";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { Role } from "@/types/rbac";
import { Search, UploadCloud, Disc } from "lucide-react";

export default function IsoImagesPage() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useStorageVolumes();
  const allVolumes = (data as any)?.volumes || [];
  // ISOs are custom storage volumes with content-type "iso".
  const isos = useMemo(
    () => allVolumes.filter((v: any) => v.content_type === "iso"),
    [allVolumes],
  );

  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const canUpload = hasRole(Role.PARTNER);

  const [searchQuery, setSearchQuery] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [uploadOpen, setUploadOpen] = useState(false);

  const sortAccessors = useMemo(
    () => ({
      name: (i: any) => i.name,
      pool: (i: any) => i.pool,
      node: (i: any) => i.node,
      used_by: (i: any) => i.used_by?.length ?? 0,
    }),
    [],
  );
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "name",
    "asc",
    () => setCurrentPage(1),
  );

  const filtered = useMemo(() => {
    if (!searchQuery) return isos;
    const q = searchQuery.toLowerCase();
    return isos.filter(
      (v: any) =>
        v.name.toLowerCase().includes(q) ||
        (v.pool && v.pool.toLowerCase().includes(q)) ||
        (v.node && v.node.toLowerCase().includes(q)),
    );
  }, [isos, searchQuery]);

  const sorted = useMemo(() => sortItems(filtered), [filtered, sortItems]);
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = sorted.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize,
  );

  if (isLoading) return <LoadingLottiePage />;

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">ISO Images</h1>
          <p className="text-muted-foreground mt-2">
            Custom ISO storage volumes that can be attached to VMs as boot media
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton
            label="Refresh ISO images"
            lastUpdatedAt={dataUpdatedAt}
            onRefresh={refreshNow}
          />
          {/* Partner+ only; the upload endpoint requires Partner. */}
          {canUpload && (
            <Button
              onClick={() => setUploadOpen(true)}
              disabled={isReadOnly}
              title={isReadOnly ? "Action disabled in read-only mode" : "Upload an ISO"}
            >
              <UploadCloud className="h-4 w-4 mr-2" />
              Upload ISO
            </Button>
          )}
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search ISO images..."
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                setCurrentPage(1);
              }}
              className="pl-10"
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            ISO Images ({filtered.length}
            {searchQuery ? ` of ${isos.length}` : ""})
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Pool" field="pool" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                <SortableTableHead label="Used By" field="used_by" currentField={sortField} direction={sortDirection} onSort={handleSort} />
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginated.length === 0 ? (
                <TableEmptyRow
                  colSpan={4}
                  icon={Disc}
                  title="No ISO images yet"
                  description={canUpload ? 'Use "Upload ISO" to add one.' : undefined}
                />
              ) : (
                paginated.map((v: any, idx: number) => (
                  <TableRow key={`${v.node}-${v.pool}-${v.name}-${idx}`}>
                    <TableCell className="font-medium">{v.name}</TableCell>
                    <TableCell>
                      {v.pool}
                    </TableCell>
                    <TableCell>
                      {v.node}
                    </TableCell>
                    <TableCell>
                      {v.used_by && v.used_by.length > 0 ? (
                        `${v.used_by.length} instance${v.used_by.length === 1 ? "" : "s"}`
                      ) : (
                        <TableEmptyValue label="Not used" />
                      )}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
          <DataTablePagination
            currentPage={currentPage}
            totalPages={totalPages}
            pageSize={pageSize}
            totalItems={sorted.length}
            onPageChange={setCurrentPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setCurrentPage(1);
            }}
          />
        </CardContent>
      </Card>

      <IsoUploadDialog
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onUploaded={() => refetch()}
      />
    </div>
  );
}
