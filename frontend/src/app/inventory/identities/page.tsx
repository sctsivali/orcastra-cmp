"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TableEmptyValue } from "@/components/ui/table-text";
import { formatLabel } from "@/lib/utils";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Search, Trash2, MoreVertical } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useIdentities, useDeleteIdentity } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

export default function IdentitiesPage() {
  const { data, isLoading, refetch, refreshNow, dataUpdatedAt } = useIdentities();
  const identities = (data as any)?.identities || [];
  const { toast } = useToast();
  // Nodes the fan-out could not reach. Their rows are absent, so say so rather
  // than let an outage read as an empty list.
  const unreachableNodes: string[] = (data as any)?.unreachable_nodes || [];
  const deleteIdentityMutation = useDeleteIdentity();
  const { isReadOnly } = useReadOnlyMode();
  
  // Pagination and search state
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [filterType, setFilterType] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  
  // Delete confirmation state
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [identityToDelete, setIdentityToDelete] = useState<any>(null);

  // Get unique nodes and types for filters
  const availableNodes = useMemo((): string[] => {
    const nodeSet = new Set<string>();
    identities.forEach((i: any) => {
      if (i.node) nodeSet.add(i.node);
    });
    return Array.from(nodeSet).sort();
  }, [identities]);

  const availableTypes = useMemo((): string[] => {
    const typeSet = new Set<string>();
    identities.forEach((i: any) => {
      if (i.type) typeSet.add(i.type);
    });
    return Array.from(typeSet).sort();
  }, [identities]);

  // Filter and search identities
  const filteredIdentities = useMemo(() => {
    return identities.filter((identity: any) => {
      const matchesSearch = 
        identity.name?.toLowerCase().includes(searchQuery.toLowerCase()) ||
        identity.id?.toLowerCase().includes(searchQuery.toLowerCase()) ||
        identity.node?.toLowerCase().includes(searchQuery.toLowerCase()) ||
        identity.groups?.some((g: string) => g.toLowerCase().includes(searchQuery.toLowerCase()));
      
      const matchesNode = filterNode === "all" || identity.node === filterNode;
      const matchesType = filterType === "all" || identity.type === filterType;
      
      return matchesSearch && matchesNode && matchesType;
    });
  }, [identities, searchQuery, filterNode, filterType]);

  // Sorting
  const sortAccessors = useMemo(() => ({
    id: (i: any) => i.id,
    name: (i: any) => i.name,
    type: (i: any) => i.type,
    node: (i: any) => i.node,
    groups: (i: any) => i.groups?.length ?? 0,
  }), []);
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "name",
    "asc",
    () => setCurrentPage(1),
  );
  const sortedIdentities = useMemo(() => sortItems(filteredIdentities), [filteredIdentities, sortItems]);

  // Pagination
  const totalPages = Math.ceil(sortedIdentities.length / pageSize);
  const paginatedIdentities = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return sortedIdentities.slice(startIndex, startIndex + pageSize);
  }, [sortedIdentities, currentPage, pageSize]);

  const handleDeleteClick = (identity: any) => {
    setIdentityToDelete(identity);
    setDeleteDialogOpen(true);
  };

  const handleDeleteConfirm = async () => {
    if (!identityToDelete) return;
    
    try {
      await deleteIdentityMutation.mutateAsync({
        authType: identityToDelete.auth_type || "tls",
        identityId: identityToDelete.id,
        host: identityToDelete.node,
      });
      toast({
        title: "Success",
        description: `Identity deleted successfully`,
      });
      refetch();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to delete identity",
        variant: "destructive",
      });
    } finally {
      setDeleteDialogOpen(false);
      setIdentityToDelete(null);
    }
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <ReadOnlyBanner />
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Identities
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor user identities
          </p>
        </div>
        <RefreshButton
          label="Refresh identities"
          lastUpdatedAt={dataUpdatedAt}
          onRefresh={refreshNow}
        />
      </div>

      <UnreachableNodesBanner noun="identities" nodes={unreachableNodes} />

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <CardTitle>All Identities ({filteredIdentities.length} of {identities.length})</CardTitle>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              <div className="lg:col-span-2">
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search identities..."
                    value={searchQuery}
                    onChange={(e) => {
                      setSearchQuery(e.target.value);
                      setCurrentPage(1);
                    }}
                    className="pl-9"
                  />
                </div>
              </div>
              <Select value={filterNode} onValueChange={(value) => { setFilterNode(value); setCurrentPage(1); }}>
                <SelectTrigger>
                  <SelectValue placeholder="All Nodes" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Nodes</SelectItem>
                  {availableNodes.map((node) => (
                    <SelectItem key={node} value={node}>{node}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={filterType} onValueChange={(value) => { setFilterType(value); setCurrentPage(1); }}>
                <SelectTrigger>
                  <SelectValue placeholder="All Types" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Types</SelectItem>
                  {availableTypes.map((type) => (
                    <SelectItem key={type} value={type}>{type.toUpperCase()}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {identities.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No identities found
            </div>
          ) : filteredIdentities.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No identities match your search criteria
            </div>
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="ID" field="id" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Name" field="name" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Type" field="type" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Groups" field="groups" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginatedIdentities.map((identity: any, index: number) => (
                    <TableRow key={identity.node + "-" + identity.id + "-" + index}>
                      <TableCell className="font-mono max-w-[200px] truncate" title={identity.id}>
                        {identity.id.length > 30 
                          ? identity.id.substring(0, 15) + "..." + identity.id.substring(identity.id.length - 10)
                          : identity.id}
                      </TableCell>
                      <TableCell className="font-medium">
                        {identity.name || <span className="text-muted-foreground italic">N/A</span>}
                      </TableCell>
                      <TableCell>
                        {formatLabel(identity.type)}
                      </TableCell>
                      <TableCell>
                        {identity.node || <TableEmptyValue label="No node" />}
                      </TableCell>
                      <TableCell>
                        <div title={(identity.groups ?? []).join(", ")}>
                          {identity.groups && identity.groups.length > 0 ? (
                            <>
                              {identity.groups.slice(0, 3).join(", ")}
                              {identity.groups.length > 3 && ` +${identity.groups.length - 3}`}
                            </>
                          ) : (
                            <TableEmptyValue label="No groups" />
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="sm">
                              <MoreVertical className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem 
                              onClick={() => handleDeleteClick(identity)}
                              className="text-destructive focus:text-destructive"
                              disabled={isReadOnly}
                            >
                              <Trash2 className="h-4 w-4 mr-2" />
                              Delete Identity
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages || 1}
                totalItems={sortedIdentities.length}
                pageSize={pageSize}
                onPageChange={setCurrentPage}
                onPageSizeChange={setPageSize}
              />
            </>
          )}
        </CardContent>
      </Card>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Identity</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete this identity? 
              This action cannot be undone and will remove this identity from the hypervisor.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setIdentityToDelete(null)}>Cancel</AlertDialogCancel>
            <AlertDialogAction 
              onClick={handleDeleteConfirm}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
