"use client";

import { useState, useEffect } from "react";
import {
  permissionResources,
  projectNamesOf,
  projectViewPrerequisites,
  resourceQualifier,
  type PermissionResource,
} from "@/components/groups/permission-resources";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Plus, X, Trash2, Loader2, Check, ChevronsUpDown, Search } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useQueryClient } from "@tanstack/react-query";
import { 
  useUpdateGroup, 
  useAddIdentityToGroup, 
  useRemoveIdentityFromGroup,
  useProjects,
  useInstances,
  useImageStores,
  useProfiles,
  useNetworks,
  useStoragePools,
  useIdentities,
  useGroup,
  useClusters
} from "@/hooks/use-lxd";
import { GroupInfo, IdentityInfo, IdentityListResponse } from "@/types/api";
import { cn } from "@/lib/utils";
import { getResourceBasePath, getValidEntitlements, getEntitlementLabel } from "@/lib/lxd-entitlements";
import { OverflowText } from "@/components/ui/overflow-text";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE, PANEL_PAGE_SIZES } from "@/lib/pagination";

interface EditGroupDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  group: GroupInfo | null;
  onRefresh: () => void;
}

interface Permission {
  entity_type: string;
  url: string;
  entitlement: string;
}

type Resource = PermissionResource;

interface GroupedIdentities {
  tls: string[];
  oidc: string[];
}

export function EditGroupDialog({ open, onOpenChange, group, onRefresh }: EditGroupDialogProps) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const updateGroupMutation = useUpdateGroup();
  const addIdentityMutation = useAddIdentityToGroup();
  const removeIdentityMutation = useRemoveIdentityFromGroup();
  
  // Track active tab to delay resource fetching
  const [activeTab, setActiveTab] = useState("basic");
  const [permissionsTabVisited, setPermissionsTabVisited] = useState(false);
  
  const [description, setDescription] = useState("");
  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [isPermissionsDirty, setIsPermissionsDirty] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(PANEL_PAGE_SIZE);
  const [newPermission, setNewPermission] = useState({
    entity_type: "project",
    entitlement: "can_view",
  });
  
  const [selectedResources, setSelectedResources] = useState<string[]>([]);
  const [allResources, setAllResources] = useState(false);
  const [resourceSearchQuery, setResourceSearchQuery] = useState("");

  // Identity management
  const [newIdentity, setNewIdentity] = useState({
    auth_type: "tls" as "tls" | "oidc",
    identity_id: "",
  });
  
  const [identitySearchOpen, setIdentitySearchOpen] = useState(false);
  const [identitySearchValue, setIdentitySearchValue] = useState("");
  const [showUnsavedWarning, setShowUnsavedWarning] = useState(false);
  const [identityToRemove, setIdentityToRemove] = useState<{authType: string, id: string} | null>(null);

  // Helper: Extract resource name from permission URL for better display
  const getResourceNameFromUrl = (url: string): string => {
    if (url === "/1.0") return "Server";
    if (url.endsWith("/*")) return "All Resources";
    const parts = url.split("/");
    return parts[parts.length - 1] || url;
  };

  // Helper: Check if permission already exists
  const isDuplicatePermission = (newPerm: Permission): boolean => {
    return permissions.some(
      p => p.entity_type === newPerm.entity_type && 
           p.url === newPerm.url && 
           p.entitlement === newPerm.entitlement
    );
  };
  
  // Get first available node as fallback if group.node is missing
  const { data: clustersData } = useClusters();
  const availableClusters = (clustersData as any)?.clusters || [];
  const fallbackNode = availableClusters[0]?.name || "";
  
  // Use group.node if available, otherwise fallback to first node
  const workingNode = group?.node || fallbackNode;
  
  // Fetch group data directly to get real-time updates
  const { data: groupData, refetch: refetchGroup } = useGroup(
    group?.name || "", 
    workingNode
  );
  
  // Use fetched group data if available, otherwise fallback to prop
  // Ensure node is set
  const currentGroup = groupData 
    ? { ...(groupData as GroupInfo), node: (groupData as GroupInfo).node || workingNode }
    : group 
    ? { ...group, node: group.node || workingNode }
    : null;
  
  // Lazy load resources based on selected entity type (only fetch when permissions tab is visited)
  const entityType = newPermission.entity_type;
  const shouldFetchResources = permissionsTabVisited;
  const { data: projectsData, isLoading: loadingProjects } = useProjects(
    shouldFetchResources && entityType === "project" ? workingNode : ""
  );
  // An undefined host used to mean "read the whole fleet"; the dialog only ever needs the
  // working node's rows of one entity type, so anything else is gated off.
  const instanceHost = shouldFetchResources && entityType === "instance" ? workingNode : undefined;
  const imageHost = shouldFetchResources && entityType === "image" ? workingNode : undefined;
  const { data: instancesData, isLoading: loadingInstances } = useInstances(instanceHost, {
    enabled: !!instanceHost,
  });
  const { data: imagesData, isLoading: loadingImages } = useImageStores(imageHost, { enabled: !!imageHost });
  const profileHost = shouldFetchResources && entityType === "profile" ? workingNode : undefined;
  const networkHost = shouldFetchResources && entityType === "network" ? workingNode : undefined;
  const poolHost = shouldFetchResources && entityType === "storage_pool" ? workingNode : undefined;
  const { data: profilesData, isLoading: loadingProfiles } = useProfiles(profileHost, undefined, {
    enabled: !!profileHost,
  });
  const { data: networksData, isLoading: loadingNetworks } = useNetworks(networkHost, undefined, {
    enabled: !!networkHost,
  });
  const { data: poolsData, isLoading: loadingPools } = useStoragePools(poolHost, { enabled: !!poolHost });
  
  // Fetch all identities using the same hook as identities page
  const { data: identitiesData, isLoading: loadingIdentities } = useIdentities(workingNode);
  const allIdentities = (identitiesData as IdentityListResponse)?.identities || [];

  // Track if dialog was just opened (to distinguish from data refetches)
  const [dialogJustOpened, setDialogJustOpened] = useState(false);
  
  // Track when dialog opens
  useEffect(() => {
    if (open) {
      setDialogJustOpened(true);
    } else {
      // Reset dirty flag and tab state when dialog closes
      setIsPermissionsDirty(false);
      setActiveTab("basic");
      setPermissionsTabVisited(false);
      // note: the dialog stays mounted between groups, so the page has to be released here.
      // This is a lifecycle reset on close, not a clamp: the clamp lives on the read path.
      setPage(1);
    }
  }, [open]);

  // Reset state ONLY when dialog first opens (not on every refetch)
  useEffect(() => {
    if (dialogJustOpened && currentGroup) {
      setDescription(currentGroup.description || "");
      setPermissions((currentGroup.permissions || []) as Permission[]);
      setIsPermissionsDirty(false);
      setDialogJustOpened(false); // Mark as handled
    }
  }, [dialogJustOpened, currentGroup]);
  
  // Get available resources based on entity type
  const getAvailableResources = (): Resource[] =>
    permissionResources(newPermission.entity_type, {
      projects: projectsData,
      instances: instancesData,
      images: imagesData,
      profiles: profilesData,
      networks: networksData,
      pools: poolsData,
    });

  const isLoadingResources = (): boolean => {
    // If workingNode is not available yet, show loading
    if (!workingNode) return true;
    
    switch (newPermission.entity_type) {
      case "project": return loadingProjects;
      case "instance": return loadingInstances;
      case "image": return loadingImages;
      case "profile": return loadingProfiles;
      case "network": return loadingNetworks;
      case "storage_pool": return loadingPools;
      default: return false;
    }
  };

  const handleEntityTypeChange = (value: string) => {
    const validEntitlements = getValidEntitlements(value);
    const defaultEntitlement = validEntitlements[0] || "can_view";
    
    setNewPermission({ 
      entity_type: value, 
      entitlement: defaultEntitlement 
    });
    setSelectedResources([]);
    setAllResources(false);
    setResourceSearchQuery("");
  };

  const handleResourceToggle = (resourcePath: string) => {
    setSelectedResources(prev => 
      prev.includes(resourcePath) 
        ? prev.filter(r => r !== resourcePath)
        : [...prev, resourcePath]
    );
  };

  // note: clamped on read rather than corrected in an effect. Removing the last permission on a
  // page would otherwise paint an empty list for a frame before snapping back.
  const totalPages = Math.max(1, Math.ceil(permissions.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  // important: every paged row carries its index into the full permissions array, never into the
  // visible page. The remove button edits that array, so a page-local index would delete the
  // wrong pending edit on any page after the first.
  const pagedPermissions = permissions
    .map((permission, index) => ({ permission, index }))
    .slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (!group) return null;

  // Save Basic Info (description only)
  const handleUpdateBasicInfo = async (e?: React.FormEvent | React.MouseEvent) => {
    if (e) e.preventDefault();

    try {
      // Only send description for Basic Info update
      await updateGroupMutation.mutateAsync({
        name: currentGroup?.name || group?.name || "",
        data: {
          description,
        },
        host: workingNode,
      });

      toast({
        title: "Success",
        description: `Description updated successfully`,
      });
      
      // Wait a bit for LXD to persist changes before refetching
      await new Promise(resolve => setTimeout(resolve, 500));
      
      // Refetch group data to get updated state from server
      await refetchGroup();
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to update group",
        variant: "destructive",
      });
    }
  };

  // Save Permissions
  const handleSavePermissions = async () => {
    try {
      await updateGroupMutation.mutateAsync({
        name: currentGroup?.name || group?.name || "",
        data: {
          permissions,
        },
        host: workingNode,
      });

      toast({
        title: "Success",
        description: `Permissions updated successfully`,
      });

      // Reset dirty flag
      setIsPermissionsDirty(false);
      
      // Wait a bit for LXD to persist changes before refetching
      await new Promise(resolve => setTimeout(resolve, 500));
      
      // Refetch group data to get updated state from server
      const result = await refetchGroup();
      
      // Sync permissions from the refetched data
      if (result.data) {
        const refreshedGroup = result.data as GroupInfo;
        setPermissions((refreshedGroup.permissions || []) as Permission[]);
      }
      
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to save permissions",
        variant: "destructive",
      });
    }
  };

  const handleAddPermission = () => {
    if (newPermission.entity_type !== "server" && !allResources && selectedResources.length === 0) {
      toast({
        title: "Validation Error",
        description: "Please select at least one resource or choose 'All Resources'",
        variant: "destructive",
      });
      return;
    }

    const newPerms: Permission[] = [];

    if (newPermission.entity_type === "server") {
      newPerms.push({
        entity_type: "server",
        url: "/1.0",
        entitlement: newPermission.entitlement,
      });
    } else if (allResources) {
      const basePath = getResourceBasePath(newPermission.entity_type);
      newPerms.push({
        entity_type: newPermission.entity_type,
        url: `${basePath}/*`,
        entitlement: newPermission.entitlement,
      });
    } else {
      const resources = getAvailableResources();
      selectedResources.forEach(resourcePath => {
        const resource = resources.find(r => r.path === resourcePath);
        if (resource) {
          newPerms.push({
            entity_type: newPermission.entity_type,
            url: resource.path,
            entitlement: newPermission.entitlement,
          });
        }
      });
    }

    if (newPerms.length > 0) {
      // Filter out duplicates
      const uniquePerms = newPerms.filter(p => !isDuplicatePermission(p));
      const duplicateCount = newPerms.length - uniquePerms.length;
      
      if (uniquePerms.length === 0) {
        toast({
          title: "Duplicate Permission",
          description: "This permission already exists in the group.",
          variant: "destructive",
        });
        return;
      }
      
      const prerequisites = projectViewPrerequisites(uniquePerms, permissions);
      setPermissions([...permissions, ...uniquePerms, ...prerequisites]);
      setIsPermissionsDirty(true);
      setSelectedResources([]);
      setAllResources(false);
      
      const viewNote = prerequisites.length > 0
        ? ` can_view on project ${projectNamesOf(prerequisites).join(", ")} added beside them, which LXD requires.`
        : "";
      const message = duplicateCount > 0 
        ? `Added ${uniquePerms.length} permission(s). ${duplicateCount} duplicate(s) skipped.${viewNote}`
        : `Added ${uniquePerms.length} permission(s).${viewNote}`;
      
      toast({
        title: "Permission Added",
        description: `${message} Click "Save Permissions" to apply.`,
      });
    }
  };

  const handleRemovePermission = (index: number) => {
    setPermissions(permissions.filter((_, i) => i !== index));
    setIsPermissionsDirty(true);
    toast({
      title: "Permission Removed",
      description: `Permission removed. Click "Save Permissions" to apply changes.`,
    });
  };

  const handleAddIdentity = async () => {
    if (!newIdentity.identity_id.trim()) {
      toast({
        title: "Validation Error",
        description: "Identity ID is required",
        variant: "destructive",
      });
      return;
    }

    if (!currentGroup || !workingNode) {
      toast({
        title: "Configuration Error",
        description: `No valid cluster available. Current: ${currentGroup?.node || 'undefined'}, fallback: ${fallbackNode || 'none'}`,
        variant: "destructive",
      });
      return;
    }

    try {
      await addIdentityMutation.mutateAsync({
        groupName: currentGroup.name,
        authType: newIdentity.auth_type,
        identityId: newIdentity.identity_id,
        host: workingNode,
      });

      toast({
        title: "Success",
        description: `Identity added to group "${currentGroup.name}"`,
      });

      setNewIdentity({
        auth_type: "tls",
        identity_id: "",
      });

      // Invalidate queries to refresh data
      await queryClient.invalidateQueries({ queryKey: ["groups"] });
      await queryClient.invalidateQueries({ queryKey: ["group"] });
      
      // Refetch this specific group to update UI immediately
      await refetchGroup();
      
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to add identity",
        variant: "destructive",
      });
    }
  };

  // Show confirmation dialog before removing identity
  const confirmRemoveIdentity = (authType: string, identityId: string) => {
    setIdentityToRemove({ authType, id: identityId });
  };

  const handleRemoveIdentity = async () => {
    if (!identityToRemove) return;
    
    const { authType, id: identityId } = identityToRemove;
    
    if (!currentGroup || !workingNode) {
      toast({
        title: "Configuration Error",
        description: "No valid cluster available",
        variant: "destructive",
      });
      setIdentityToRemove(null);
      return;
    }

    try {
      await removeIdentityMutation.mutateAsync({
        groupName: currentGroup.name,
        authType,
        identityId,
        host: workingNode,
      });

      toast({
        title: "Success",
        description: `Identity removed from group "${currentGroup.name}"`,
      });

      // Invalidate queries to refresh data
      await queryClient.invalidateQueries({ queryKey: ["groups"] });
      await queryClient.invalidateQueries({ queryKey: ["group"] });
      
      // Refetch this specific group to update UI immediately
      await refetchGroup();
      
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to remove identity",
        variant: "destructive",
      });
    } finally {
      setIdentityToRemove(null);
    }
  };

  // Get identities from currentGroup - now properly structured as {tls: [], oidc: []}
  const groupedIdentities: GroupedIdentities = {
    tls: currentGroup?.identities?.tls || [],
    oidc: currentGroup?.identities?.oidc || [],
  };

  if (!currentGroup) return null;

  // Handle dialog close with unsaved changes check
  const handleDialogChange = (isOpen: boolean) => {
    if (!isOpen && isPermissionsDirty) {
      setShowUnsavedWarning(true);
      return;
    }
    onOpenChange(isOpen);
  };

  const handleDiscardChanges = () => {
    setShowUnsavedWarning(false);
    setIsPermissionsDirty(false);
    onOpenChange(false);
  };

  const handleSaveAndClose = async () => {
    setShowUnsavedWarning(false);
    await handleUpdateBasicInfo();
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={handleDialogChange}>
      <DialogContent className="max-w-3xl max-h-[80vh] flex flex-col overflow-hidden">
        <DialogHeader className="flex-shrink-0 pb-4">
          <DialogTitle>
            Edit Group: {currentGroup.name}
          </DialogTitle>
          <DialogDescription>
            Update group details, permissions, and manage TLS/OIDC identities
          </DialogDescription>
        </DialogHeader>

        <Tabs value={activeTab} onValueChange={(value) => {
          setActiveTab(value);
          if (value === "permissions") setPermissionsTabVisited(true);
        }} className="w-full flex flex-col flex-1 min-h-0">
          <TabsList className="grid w-full grid-cols-3 flex-shrink-0 h-auto p-1">
            <TabsTrigger value="basic" className="text-[10px] sm:text-xs px-1.5 sm:px-3 py-1.5">Basic Info</TabsTrigger>
            <TabsTrigger value="permissions" className="text-[10px] sm:text-xs px-1.5 sm:px-3 py-1.5">Permissions</TabsTrigger>
            <TabsTrigger value="identities" className="text-[10px] sm:text-xs px-1.5 sm:px-3 py-1.5 truncate">Identities</TabsTrigger>
          </TabsList>

          {/* Basic Info Tab */}
          <TabsContent value="basic" className="space-y-4 overflow-y-auto flex-1 min-h-0 pr-2">
            <form onSubmit={handleUpdateBasicInfo} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="group-name">Group Name</Label>
                <Input
                  id="group-name"
                  value={currentGroup.name}
                  disabled
                  className="bg-muted"
                />
                <p className="text-xs text-muted-foreground">
                  Group name cannot be changed
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="description">Description</Label>
                <Textarea
                  id="description"
                  placeholder="Describe the purpose of this group"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={3}
                />
              </div>

              <div className="space-y-2">
                <Label>Cluster</Label>
                <Input value={currentGroup.node} disabled className="bg-muted" />
              </div>

              <DialogFooter>
                <Button
                  type="submit"
                  disabled={updateGroupMutation.isPending}
                >
                  {updateGroupMutation.isPending ? "Saving..." : "Save Changes"}
                </Button>
              </DialogFooter>
            </form>
          </TabsContent>

          {/* Permissions Tab */}
          <TabsContent value="permissions" className="space-y-3 overflow-y-auto flex-1 min-h-0 pr-2">
            <div>
              <Label className="text-sm font-semibold">Group Permissions</Label>
              <p className="text-xs text-muted-foreground">
                Manage what resources this group can access
              </p>
            </div>

            {/* Existing Permissions */}
            {permissions.length > 0 && (
              <div className="space-y-2">
                {pagedPermissions.map(({ permission, index }) => (
                  <div
                    key={`${permission.entity_type}-${permission.url}-${permission.entitlement}-${index}`}
                    className="flex items-center gap-2 p-3 bg-secondary/50 rounded-lg group hover:bg-secondary/70 transition-colors"
                  >
                    <Badge variant="outline" className="min-w-[90px] capitalize">
                      {permission.entity_type.replace('_', ' ')}
                    </Badge>
                    <div className="flex-1 min-w-0">
                      <span className="font-medium text-sm">
                        {getResourceNameFromUrl(permission.url)}
                      </span>
                      <span className="font-mono text-xs text-muted-foreground ml-2 hidden sm:inline">
                        {permission.url !== "/1.0" && permission.url}
                      </span>
                    </div>
                    <Badge 
                      variant={permission.entitlement === 'operator' || permission.entitlement === 'admin' ? 'default' : 'secondary'}
                      className="min-w-[80px]"
                    >
                      {getEntitlementLabel(permission.entitlement).split(' (')[0]}
                    </Badge>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="opacity-50 group-hover:opacity-100 transition-opacity"
                      onClick={() => handleRemovePermission(index)}
                      title="Remove permission"
                    >
                      <X className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {/* Absent rather than disabled when every permission fits on one page. No mt-4
                wrapper: the tab is already a space-y-3 stack, whose gap wins over a margin. */}
            {totalPages > 1 && (
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                pageSize={pageSize}
                pageSizeOptions={[...PANEL_PAGE_SIZES]}
                totalItems={permissions.length}
                onPageChange={setPage}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setPage(1);
                }}
              />
            )}

            {permissions.length === 0 && (
              <div className="text-center py-8 text-muted-foreground border rounded-lg">
                No permissions configured
              </div>
            )}

            {/* Add New Permission */}
            <div className="border rounded-lg p-4 space-y-4">
              <Label className="text-sm font-medium">Add Permission</Label>
              
              {/* Step 1: Select Resource Type */}
              <div className="space-y-2">
                <Label className="text-xs text-muted-foreground">1. Select Resource Type</Label>
                <Select
                  value={newPermission.entity_type}
                  onValueChange={handleEntityTypeChange}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="project">Projects</SelectItem>
                    <SelectItem value="instance">Instances (Containers/VMs)</SelectItem>
                    <SelectItem value="image">Images</SelectItem>
                    <SelectItem value="profile">Profiles</SelectItem>
                    <SelectItem value="network">Networks</SelectItem>
                    <SelectItem value="storage_pool">Storage Pools</SelectItem>
                    <SelectItem value="server">Server (Full Access)</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {/* Step 2: Select Resources */}
              {newPermission.entity_type !== "server" && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label className="text-xs text-muted-foreground">
                      2. Select Resources from {workingNode}
                    </Label>
                    {isLoadingResources() && (
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        <Loader2 className="h-3 w-3 animate-spin" />
                        <span>Loading...</span>
                      </div>
                    )}
                  </div>

                  {/* All Resources Checkbox */}
                  <div className="flex items-center gap-2 p-2 bg-muted/50 rounded border">
                    <Checkbox
                      id="all-resources-edit"
                      checked={allResources}
                      onCheckedChange={(checked) => {
                        const isChecked = checked === true;
                        setAllResources(isChecked);
                        if (isChecked) setSelectedResources([]);
                      }}
                      className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0"
                    />
                    <Label
                      htmlFor="all-resources-edit"
                      className="text-xs font-medium cursor-pointer flex-1"
                    >
                      All {newPermission.entity_type}s (*)
                    </Label>
                  </div>

                  {/* Resource List */}
                  {!allResources && (
                    <div className="space-y-2">
                      {/* Search Input */}
                      <div className="relative">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                        <input
                          type="text"
                          placeholder={`Search ${newPermission.entity_type}s...`}
                          value={resourceSearchQuery}
                          onChange={(e) => setResourceSearchQuery(e.target.value)}
                          className="w-full h-9 pl-9 pr-3 text-sm border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                        />
                      </div>
                      <div className="border rounded-lg max-h-40 overflow-y-auto bg-background">
                        {isLoadingResources() ? (
                          <div className="p-4 text-center text-xs text-muted-foreground">
                            <Loader2 className="h-4 w-4 animate-spin mx-auto mb-1" />
                            Loading {newPermission.entity_type}s...
                          </div>
                        ) : getAvailableResources().length === 0 ? (
                          <div className="p-4 text-center text-xs text-muted-foreground">
                            No {newPermission.entity_type}s found
                          </div>
                        ) : (
                          <div className="p-2 space-y-1">
                            {getAvailableResources()
                              .filter((resource) => 
                                resourceSearchQuery === "" || 
                                resource.name.toLowerCase().includes(resourceSearchQuery.toLowerCase()) ||
                                (resource.project && resource.project.toLowerCase().includes(resourceSearchQuery.toLowerCase()))
                              )
                              .map((resource) => (
                                <div
                                  key={resource.path}
                                  className="flex items-center gap-2 p-1.5 hover:bg-accent rounded transition-colors"
                                >
                                  <Checkbox
                                    id={`edit-resource-${resource.path}`}
                                    checked={selectedResources.includes(resource.path)}
                                    onCheckedChange={(checked) => {
                                      if (checked === true || checked === false) {
                                        handleResourceToggle(resource.path);
                                      }
                                    }}
                                    className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0"
                                  />
                                  <Label 
                                    htmlFor={`edit-resource-${resource.path}`}
                                    className="text-xs cursor-pointer font-medium flex-1 break-words"
                                  >
                                    {resource.name}
                                    {resourceQualifier(newPermission.entity_type, resource.project) && (
                                      <span className="text-xs text-muted-foreground ml-1">({resourceQualifier(newPermission.entity_type, resource.project)})</span>
                                    )}
                                  </Label>
                                </div>
                              ))}
                              {getAvailableResources().filter((resource) => 
                                resourceSearchQuery === "" || 
                                resource.name.toLowerCase().includes(resourceSearchQuery.toLowerCase()) ||
                                (resource.project && resource.project.toLowerCase().includes(resourceSearchQuery.toLowerCase()))
                              ).length === 0 && resourceSearchQuery !== "" && (
                                <div className="p-4 text-center text-xs text-muted-foreground">
                                  No {newPermission.entity_type}s matching "{resourceSearchQuery}"
                                </div>
                              )}
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {selectedResources.length > 0 && !allResources && (
                    <div className="text-xs text-muted-foreground bg-muted/50 p-2 rounded">
                      ✓ {selectedResources.length} resource(s) selected
                    </div>
                  )}
                </div>
              )}

              {/* Step 3: Permission Level */}
              <div className="space-y-2">
                <Label className="text-xs text-muted-foreground">
                  {newPermission.entity_type === "server" ? "2" : "3"}. Set Permission Level
                </Label>
                <Select
                  value={newPermission.entitlement}
                  onValueChange={(value) =>
                    setNewPermission({ ...newPermission, entitlement: value })
                  }
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {getValidEntitlements(newPermission.entity_type).map((entitlement) => (
                      <SelectItem key={entitlement} value={entitlement}>
                        {getEntitlementLabel(entitlement)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Only valid permissions for {newPermission.entity_type}s are shown
                </p>
              </div>

              <Button
                type="button"
                onClick={handleAddPermission}
                className="w-full"
                variant="outline"
                disabled={isLoadingResources()}
              >
                <Plus className="h-4 w-4 mr-2" />
                Add Permission
              </Button>
            </div>

            <DialogFooter className="flex items-center gap-2">
              {isPermissionsDirty && (
                <span className="text-xs text-amber-600 dark:text-amber-400 mr-auto flex items-center gap-1">
                  <span className="w-2 h-2 rounded-full bg-status-pending animate-pulse"></span>
                  Unsaved changes
                </span>
              )}
              <Button
                onClick={handleSavePermissions}
                disabled={updateGroupMutation.isPending}
                variant={isPermissionsDirty ? "default" : "secondary"}
              >
                {updateGroupMutation.isPending ? "Saving..." : "Save Permissions"}
              </Button>
            </DialogFooter>
          </TabsContent>

          {/* Identities Tab */}
          <TabsContent value="identities" className="space-y-3 overflow-y-auto flex-1 min-h-0 pr-2">
            <div>
              <Label className="text-base font-semibold">Group Members (Identities)</Label>
              <p className="text-sm text-muted-foreground mt-1">
                Add TLS certificates or OIDC users to this group
              </p>
            </div>

            {/* TLS Identities */}
            <div className="space-y-2">
              <Label className="text-sm font-semibold">
                TLS Identities ({groupedIdentities.tls.length})
              </Label>
              {groupedIdentities.tls.length > 0 ? (
                <div className="space-y-2">
                  {groupedIdentities.tls.map((identityId, index) => {
                    // Find the identity details to get the name
                    const identityDetails = allIdentities.find(i => i.id === identityId);
                    const identityName = identityDetails?.name || '';
                    
                    return (
                      <div
                        key={index}
                        className="flex items-center justify-between p-3 bg-secondary/50 rounded-lg group hover:bg-secondary/70 transition-colors"
                      >
                        <div className="flex flex-col flex-1 min-w-0">
                          {identityName && (
                            <OverflowText
                              value={identityName}
                              label={`name for identity ${identityId}`}
                              className="text-sm font-medium"
                            />
                          )}
                          <span className="text-xs text-muted-foreground">
                            <OverflowText value={identityId} label="identity id" reveal="always" mono />
                          </span>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => confirmRemoveIdentity("tls", identityId)}
                          disabled={removeIdentityMutation.isPending}
                          className="opacity-50 group-hover:opacity-100 transition-opacity ml-2"
                          title="Remove identity"
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground italic py-2">
                  No TLS identities
                </p>
              )}
            </div>

            {/* OIDC Identities */}
            <div className="space-y-2">
              <Label className="text-sm font-semibold">
                OIDC Identities ({groupedIdentities.oidc.length})
              </Label>
              {groupedIdentities.oidc.length > 0 ? (
                <div className="space-y-2">
                  {groupedIdentities.oidc.map((identityId, index) => {
                    // Find the identity details to get the name
                    const identityDetails = allIdentities.find(i => i.id === identityId);
                    const identityName = identityDetails?.name || '';
                    
                    return (
                      <div
                        key={index}
                        className="flex items-center justify-between p-3 bg-secondary/50 rounded-lg group hover:bg-secondary/70 transition-colors"
                      >
                        <div className="flex flex-col flex-1 min-w-0">
                          {identityName && (
                            <OverflowText
                              value={identityName}
                              label={`name for identity ${identityId}`}
                              className="text-sm font-medium"
                            />
                          )}
                          <span className="text-xs text-muted-foreground">
                            <OverflowText value={identityId} label="identity id" reveal="always" mono />
                          </span>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => confirmRemoveIdentity("oidc", identityId)}
                          disabled={removeIdentityMutation.isPending}
                          className="opacity-50 group-hover:opacity-100 transition-opacity ml-2"
                          title="Remove identity"
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground italic py-2">
                  No OIDC identities
                </p>
              )}
            </div>

            {/* Add New Identity */}
            <div className="border rounded-lg p-4 space-y-3">
              <Label className="text-sm font-medium">Add Identity</Label>
              
              {loadingIdentities ? (
                <div className="flex items-center justify-center py-6">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                  <span className="ml-2 text-sm text-muted-foreground">Loading identities...</span>
                </div>
              ) : (
                <>
                  {/* Filter by Auth Type */}
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">Filter by Type</Label>
                    <Select
                      value={newIdentity.auth_type}
                      onValueChange={(value: "tls" | "oidc") => {
                        setNewIdentity({ ...newIdentity, auth_type: value, identity_id: "" });
                        setIdentitySearchValue("");
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="tls">TLS Identities</SelectItem>
                        <SelectItem value="oidc">OIDC Identities</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>

                  {/* Searchable Identity List */}
                  <div className="space-y-2">
                    <Label className="text-xs text-muted-foreground">
                      Select Identity from {workingNode}
                    </Label>
                    <Popover open={identitySearchOpen} onOpenChange={setIdentitySearchOpen} modal={true}>
                      <PopoverTrigger asChild>
                        <Button
                          variant="outline"
                          role="combobox"
                          aria-expanded={identitySearchOpen}
                          className="w-full justify-between h-auto min-h-[2.5rem]"
                        >
                          {newIdentity.identity_id ? (
                            <span className="truncate text-left flex-1 font-mono text-xs">
                              {allIdentities.find(i => i.id === newIdentity.identity_id)?.name || newIdentity.identity_id}
                            </span>
                          ) : (
                            <span className="text-muted-foreground">Search and select identity...</span>
                          )}
                          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                        </Button>
                      </PopoverTrigger>
                      <PopoverContent className="p-0" align="start" style={{ width: 'var(--radix-popover-trigger-width)' }}>
                        <Command shouldFilter={false}>
                          <CommandInput 
                            placeholder={`Search ${newIdentity.auth_type.toUpperCase()} identities...`}
                            value={identitySearchValue}
                            onValueChange={setIdentitySearchValue}
                          />
                          <CommandList>
                            <CommandEmpty>
                              {loadingIdentities ? "Loading identities..." : "No identities found."}
                            </CommandEmpty>
                            <CommandGroup>
                              {(allIdentities || [])
                                .filter(identity => {
                                  // Map API types to auth_type
                                  // TLS: "Client certificate", "Client certificate (pending)", etc.
                                  // OIDC: "OIDC client"
                                  const isTLS = identity.type?.toLowerCase().includes('certificate');
                                  const isOIDC = identity.type?.toLowerCase().includes('oidc');
                                  
                                  if (newIdentity.auth_type === 'tls') {
                                    return isTLS;
                                  } else if (newIdentity.auth_type === 'oidc') {
                                    return isOIDC;
                                  }
                                  return false;
                                })
                                .filter(identity => {
                                  // Don't show identities already in this group
                                  const tlsIdentities = currentGroup?.identities?.tls || [];
                                  const oidcIdentities = currentGroup?.identities?.oidc || [];
                                  const allGroupIdentities = [...tlsIdentities, ...oidcIdentities];
                                  return !allGroupIdentities.includes(identity.id);
                                })
                                .filter(identity => {
                                  // Custom search filter - only search in name and ID
                                  if (!identitySearchValue) return true;
                                  
                                  const searchLower = identitySearchValue.toLowerCase();
                                  const nameMatch = identity.name?.toLowerCase().includes(searchLower);
                                  const idMatch = identity.id?.toLowerCase().includes(searchLower);
                                  
                                  return nameMatch || idMatch;
                                })
                                .map((identity) => (
                                  <CommandItem
                                    key={identity.id}
                                    value={identity.id}
                                    onSelect={() => {
                                      setNewIdentity({ 
                                        ...newIdentity, 
                                        identity_id: identity.id 
                                      });
                                      setIdentitySearchOpen(false);
                                      setIdentitySearchValue("");
                                    }}
                                  >
                                    <Check
                                      className={cn(
                                        "mr-2 h-4 w-4",
                                        newIdentity.identity_id === identity.id ? "opacity-100" : "opacity-0"
                                      )}
                                    />
                                    <div className="min-w-0 flex-1">
                                      <div className="break-words text-sm font-medium">
                                        {identity.name || EMPTY_VALUE}
                                      </div>
                                      <div className="break-all font-mono text-xs text-muted-foreground">
                                        {identity.id}
                                      </div>
                                      {identity.groups.length > 0 && (
                                        <div className="text-xs text-muted-foreground mt-1">
                                          Groups: {identity.groups.join(", ")}
                                        </div>
                                      )}
                                    </div>
                                  </CommandItem>
                                ))}
                            </CommandGroup>
                          </CommandList>
                        </Command>
                      </PopoverContent>
                    </Popover>
                  </div>

                  {/* Add Button */}
                  <Button
                    type="button"
                    onClick={handleAddIdentity}
                    disabled={addIdentityMutation.isPending || !newIdentity.identity_id}
                    className="w-full"
                  >
                    {addIdentityMutation.isPending ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        Adding...
                      </>
                    ) : (
                      "Add Selected Identity"
                    )}
                  </Button>

                  {/* Info Text */}
                  <div className="bg-muted/50 rounded p-3 space-y-1">
                    <p className="text-xs text-muted-foreground">
                      <strong>Tip:</strong> Use the search box to quickly find identities by name or ID.
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Showing {newIdentity.auth_type.toUpperCase()} identities available on <strong>{workingNode}</strong>
                    </p>
                  </div>
                </>
              )}
            </div>
          </TabsContent>
        </Tabs>
      </DialogContent>

      {/* Unsaved Changes Warning Dialog */}
      <Dialog open={showUnsavedWarning} onOpenChange={setShowUnsavedWarning}>
        <DialogContent className="max-w-md">
          <DialogHeader className="pb-4">
            <DialogTitle className="text-amber-600">
              Unsaved Changes
            </DialogTitle>
            <DialogDescription>
              You have unsaved permission changes. Would you like to save them before closing?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="flex gap-2 sm:gap-0">
            <Button variant="outline" onClick={handleDiscardChanges}>
              Discard Changes
            </Button>
            <Button onClick={handleSaveAndClose} disabled={updateGroupMutation.isPending}>
              {updateGroupMutation.isPending ? "Saving..." : "Save & Close"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Remove Identity Confirmation Dialog */}
      <Dialog open={!!identityToRemove} onOpenChange={(open) => !open && setIdentityToRemove(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader className="pb-4">
            <DialogTitle className="text-destructive">
              Remove Identity
            </DialogTitle>
            <DialogDescription className="space-y-2">
              <span>Are you sure you want to remove this identity from the group?</span>
              {/* important: printed whole, and not behind a reveal. This is the identity the
                  operator is about to remove, and asking them to click something to see what they
                  are confirming is how the wrong one gets removed. */}
              <span className="block max-w-full break-all rounded bg-muted px-2 py-1.5 font-mono text-xs font-medium">
                {identityToRemove?.id}
              </span>
              <span className="text-destructive font-medium">This action cannot be undone.</span>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="flex gap-2 sm:gap-0">
            <Button variant="outline" onClick={() => setIdentityToRemove(null)}>
              Cancel
            </Button>
            <Button 
              variant="destructive" 
              onClick={handleRemoveIdentity} 
              disabled={removeIdentityMutation.isPending}
            >
              {removeIdentityMutation.isPending ? "Removing..." : "Remove Identity"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Dialog>
  );
}
