"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { 
  Users, 
  RotateCw, 
  UserCheck, 
  Shield, 
  Plus, 
  Edit, 
  Trash2, 
  MoreVertical,
  Search,
  Loader2,
} from "lucide-react";
import { GroupInfo, IdentityInfo, IdentityListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { useGroups, useDeleteGroup, useIdentities } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { CreateGroupDialog } from "@/components/groups/create-group-dialog";
import { EditGroupDialog } from "@/components/groups/edit-group-dialog";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { ReadOnlyBanner } from "@/components/read-only-banner";

export default function GroupsPage() {
  const { data, isLoading, refetch, isRefetching } = useGroups();
  const groups = (data as any)?.groups || [];
  const nodes = (data as any)?.nodes || [];
  const { toast } = useToast();
  const deleteGroupMutation = useDeleteGroup();
  const { isReadOnly } = useReadOnlyMode();
  
  // Pagination and search state
  const [searchQuery, setSearchQuery] = useState("");
  const [filterNode, setFilterNode] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  
  // State for modals
  const [identitiesModalOpen, setIdentitiesModalOpen] = useState(false);
  const [permissionsModalOpen, setPermissionsModalOpen] = useState(false);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState<any>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [groupToDelete, setGroupToDelete] = useState<any>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  // Get unique nodes for filter
  const availableNodes = useMemo((): string[] => {
    const nodeSet = new Set<string>();
    groups.forEach((g: any) => {
      if (g.node) nodeSet.add(g.node as string);
    });
    return Array.from(nodeSet).sort();
  }, [groups]);

  // Fetch identities for the selected group's node (for view members modal)
  const { data: identitiesData } = useIdentities(selectedGroup?.node || "");
  const allIdentities: IdentityInfo[] = (identitiesData as IdentityListResponse)?.identities || [];
  
  // Helper to find identity name from ID
  const getIdentityName = (identityId: string): string | null => {
    const identity = allIdentities.find(i => i.id === identityId);
    return identity?.name || null;
  };

  // Filter and search groups
  const filteredGroups = useMemo(() => {
    return groups.filter((group: any) => {
      const matchesSearch = 
        group.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (group.description && group.description.toLowerCase().includes(searchQuery.toLowerCase())) ||
        (group.node && group.node.toLowerCase().includes(searchQuery.toLowerCase()));
      
      const matchesNode = filterNode === "all" || group.node === filterNode;
      
      return matchesSearch && matchesNode;
    });
  }, [groups, searchQuery, filterNode]);

  // Pagination
  const totalPages = Math.ceil(filteredGroups.length / pageSize);
  const paginatedGroups = useMemo(() => {
    const startIndex = (currentPage - 1) * pageSize;
    return filteredGroups.slice(startIndex, startIndex + pageSize);
  }, [filteredGroups, currentPage, pageSize]);

  const handleShowIdentities = (group: any) => {
    setSelectedGroup(group);
    setIdentitiesModalOpen(true);
  };

  const handleShowPermissions = (group: any) => {
    setSelectedGroup(group);
    setPermissionsModalOpen(true);
  };

  const handleEdit = (group: any) => {
    setSelectedGroup(group);
    setEditDialogOpen(true);
  };

  const handleDelete = async (group: any) => {
    setGroupToDelete(group);
    setDeleteDialogOpen(true);
  };

  const confirmDelete = async () => {
    if (!groupToDelete) return;
    setIsDeleting(true);

    try {
      await deleteGroupMutation.mutateAsync({
        name: groupToDelete.name,
        host: groupToDelete.node,
      });

      toast({
        title: "Success",
        description: `Group "${groupToDelete.name}" deleted successfully`,
      });

      refetch();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to delete group",
        variant: "destructive",
      });
    } finally {
      setIsDeleting(false);
      setDeleteDialogOpen(false);
      setGroupToDelete(null);
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
            Groups
          </h1>
          <p className="text-muted-foreground mt-2">
            Manage hypervisor user groups with TLS/OIDC identities and permissions
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Button onClick={() => refetch()} disabled={isRefetching} variant="outline" size="sm" className="gap-2">
            <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
            <span className="hidden sm:inline">Refresh</span>
          </Button>
          <Button 
            onClick={() => setCreateDialogOpen(true)} 
            size="sm" 
            className="gap-2"
            disabled={isReadOnly}
            title={isReadOnly ? "Disabled in read-only mode" : undefined}
          >
            <Plus className="h-4 w-4" />
            <span className="hidden sm:inline">Create Group</span>
            <span className="sm:hidden">Create</span>
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <CardTitle>All Groups ({filteredGroups.length} of {groups.length})</CardTitle>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              <div className="lg:col-span-2">
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    placeholder="Search groups..."
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
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {groups.length === 0 ? (
            <div className="text-center py-12">
              <Users className="h-12 w-12 mx-auto text-muted-foreground mb-4" />
              <h3 className="text-lg font-semibold mb-2">No groups found</h3>
              <p className="text-muted-foreground mb-4">
                Create your first group to manage permissions and identities
              </p>
              <Button onClick={() => setCreateDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-2" />
                Create Group
              </Button>
            </div>
          ) : (
            <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Node</TableHead>
                  <TableHead>Identities</TableHead>
                  <TableHead>Permissions</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {paginatedGroups.map((group: any) => (
                  <TableRow key={`${group.node}-${group.name}`}>
                    <TableCell className="font-medium">{group.name}</TableCell>
                    <TableCell className="max-w-xs truncate">
                      {group.description || <span className="text-muted-foreground italic">No description</span>}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{group.node || "N/A"}</Badge>
                    </TableCell>
                    <TableCell className="text-center">
                      <Badge 
                        variant="secondary" 
                        className="cursor-pointer hover:bg-secondary/80 transition-colors"
                        onClick={() => handleShowIdentities(group)}
                      >
                        {((group.identities?.tls?.length || 0) + (group.identities?.oidc?.length || 0))} members
                      </Badge>
                    </TableCell>
                    <TableCell className="text-center">
                      <Badge 
                        variant="outline"
                        className="cursor-pointer hover:bg-accent transition-colors"
                        onClick={() => handleShowPermissions(group)}
                      >
                        {group.permissions?.length || 0} permissions
                      </Badge>
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
                            onClick={() => handleEdit(group)}
                            disabled={isReadOnly}
                          >
                            <Edit className="h-4 w-4 mr-2" />
                            Edit Group
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleShowIdentities(group)}>
                            <UserCheck className="h-4 w-4 mr-2" />
                            View Members
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleShowPermissions(group)}>
                            <Shield className="h-4 w-4 mr-2" />
                            View Permissions
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem 
                            onClick={() => handleDelete(group)}
                            className="text-destructive focus:text-destructive"
                            disabled={isReadOnly}
                          >
                            <Trash2 className="h-4 w-4 mr-2" />
                            Delete Group
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
              totalItems={filteredGroups.length}
              pageSize={pageSize}
              onPageChange={setCurrentPage}
              onPageSizeChange={setPageSize}
            />
            </>
          )}
        </CardContent>
      </Card>

      {/* Create Group Dialog */}
      <CreateGroupDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
        nodes={nodes}
      />

      {/* Edit Group Dialog */}
      <EditGroupDialog
        open={editDialogOpen}
        onOpenChange={setEditDialogOpen}
        group={selectedGroup}
        onRefresh={refetch}
      />

      {/* Identities Modal */}
      <Dialog open={identitiesModalOpen} onOpenChange={setIdentitiesModalOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              Group Members: {selectedGroup?.name}
            </DialogTitle>
            <DialogDescription>
              {((selectedGroup?.identities?.tls?.length || 0) + (selectedGroup?.identities?.oidc?.length || 0))} member(s) in this group
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-4">
            {/* TLS Identities */}
            {selectedGroup?.identities?.tls?.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2 flex items-center gap-2">
                  <Shield className="h-4 w-4" />
                  TLS Identities ({selectedGroup.identities.tls.length})
                </h4>
                <div className="space-y-2">
                  {selectedGroup.identities.tls.map((identity: string, index: number) => {
                    const identityName = getIdentityName(identity);
                    return (
                      <div 
                        key={`tls-${index}`}
                        className="flex flex-col gap-1 p-3 rounded-lg bg-secondary/50 hover:bg-secondary transition-colors"
                      >
                        <div className="flex items-center gap-2">
                          <Shield className="h-4 w-4 text-muted-foreground shrink-0" />
                          <span className="font-medium text-sm">
                            {identityName || "N/A"}
                          </span>
                        </div>
                        <span className="font-mono text-xs text-muted-foreground ml-6">
                          {identity}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            
            {/* OIDC Identities */}
            {selectedGroup?.identities?.oidc?.length > 0 && (
              <div>
                <h4 className="text-sm font-semibold mb-2 flex items-center gap-2">
                  <Users className="h-4 w-4" />
                  OIDC Identities ({selectedGroup.identities.oidc.length})
                </h4>
                <div className="space-y-2">
                  {selectedGroup.identities.oidc.map((identity: string, index: number) => {
                    const identityName = getIdentityName(identity);
                    return (
                      <div 
                        key={`oidc-${index}`}
                        className="flex items-center gap-2 p-3 rounded-lg bg-secondary/50 hover:bg-secondary transition-colors"
                      >
                        <Users className="h-4 w-4 text-muted-foreground shrink-0" />
                        <span className="font-medium text-sm">
                          {identityName || identity}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            
            {/* No members */}
            {(!selectedGroup?.identities?.tls?.length && !selectedGroup?.identities?.oidc?.length) && (
              <div className="text-center py-8 text-muted-foreground">
                No members in this group
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Permissions Modal */}
      <Dialog open={permissionsModalOpen} onOpenChange={setPermissionsModalOpen}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              Permissions: {selectedGroup?.name}
            </DialogTitle>
            <DialogDescription>
              {selectedGroup?.permissions?.length || 0} permission(s) configured for this group
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4">
            {selectedGroup?.permissions?.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Entity Type</TableHead>
                    <TableHead>Resource URL</TableHead>
                    <TableHead>Entitlement</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {selectedGroup.permissions.map((permission: any, index: number) => (
                    <TableRow key={index}>
                      <TableCell>
                        <Badge variant="outline">{permission.entity_type || "N/A"}</Badge>
                      </TableCell>
                      <TableCell className="font-mono text-xs max-w-md truncate">
                        {permission.url || "N/A"}
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">{permission.entitlement || "N/A"}</Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="text-center py-8 text-muted-foreground">
                No permissions configured for this group
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete Group Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={(open) => {
        if (!open && !isDeleting) {
          setDeleteDialogOpen(false);
          setGroupToDelete(null);
        }
      }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-destructive">Delete Group</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete group
              <strong className="text-foreground"> {groupToDelete?.name}</strong>
              {groupToDelete?.node && (
                <> from node <strong className="text-foreground">{groupToDelete.node}</strong></>
              )}?
              <span className="block mt-2 text-muted-foreground text-xs">
                This action cannot be undone. All identities and permissions assigned to this group will be removed.
              </span>
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-3 mt-4">
            <Button
              variant="outline"
              onClick={() => {
                setDeleteDialogOpen(false);
                setGroupToDelete(null);
              }}
              disabled={isDeleting}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={confirmDelete}
              disabled={isDeleting}
            >
              {isDeleting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin mr-2" />
                  Deleting...
                </>
              ) : (
                "Delete Group"
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
