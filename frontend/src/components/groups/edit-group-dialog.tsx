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
import { cn, plural } from "@/lib/utils";
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
  
  // Resource reads wait until the Permissions tab is first visited.
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

  const [newIdentity, setNewIdentity] = useState({
    auth_type: "tls" as "tls" | "oidc",
    identity_id: "",
  });
  
  const [identitySearchOpen, setIdentitySearchOpen] = useState(false);
  const [identitySearchValue, setIdentitySearchValue] = useState("");
  const [showUnsavedWarning, setShowUnsavedWarning] = useState(false);
  const [identityToRemove, setIdentityToRemove] = useState<{authType: string, id: string} | null>(null);

  const getResourceNameFromUrl = (url: string): string => {
    if (url === "/1.0") return "Server";
    if (url.endsWith("/*")) return "All Resources";
    const parts = url.split("/");
    return parts[parts.length - 1] || url;
  };

  const isDuplicatePermission = (newPerm: Permission): boolean => {
    return permissions.some(
      p => p.entity_type === newPerm.entity_type && 
           p.url === newPerm.url && 
           p.entitlement === newPerm.entitlement
    );
  };
  
  // Falls back to the first cluster when the group carries no node.
  const { data: clustersData } = useClusters();
  const availableClusters = (clustersData as any)?.clusters || [];
  const fallbackNode = availableClusters[0]?.name || "";
  
  const workingNode = group?.node || fallbackNode;
  
  // Read the group directly so saved changes show without reopening the dialog.
  const { data: groupData, refetch: refetchGroup } = useGroup(
    group?.name || "", 
    workingNode
  );
  
  const currentGroup = groupData 
    ? { ...(groupData as GroupInfo), node: (groupData as GroupInfo).node || workingNode }
    : group 
    ? { ...group, node: group.node || workingNode }
    : null;
  
  // Only the selected entity type is read, and only once the Permissions tab has been visited.
  const entityType = newPermission.entity_type;
  const shouldFetchResources = permissionsTabVisited;
  const { data: projectsData, isLoading: loadingProjects } = useProjects(
    shouldFetchResources && entityType === "project" ? workingNode : ""
  );
  // The dialog needs only the working node's rows of one entity type, so every other read is
  // disabled.
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
  
  const { data: identitiesData, isLoading: loadingIdentities } = useIdentities(workingNode);
  const allIdentities = (identitiesData as IdentityListResponse)?.identities || [];

  const [dialogJustOpened, setDialogJustOpened] = useState(false);
  
  useEffect(() => {
    if (open) {
      setDialogJustOpened(true);
    } else {
      setIsPermissionsDirty(false);
      setActiveTab("basic");
      setPermissionsTabVisited(false);
      // The dialog stays mounted between groups, so the page is reset on close.
      setPage(1);
    }
  }, [open]);

  // Seed the form once per opening, so a refetch never overwrites edits in progress.
  useEffect(() => {
    if (dialogJustOpened && currentGroup) {
      setDescription(currentGroup.description || "");
      setPermissions((currentGroup.permissions || []) as Permission[]);
      setIsPermissionsDirty(false);
      setDialogJustOpened(false);
    }
  }, [dialogJustOpened, currentGroup]);
  
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

  // Clamped on read rather than corrected in an effect. Removing the last permission on a page
  // would otherwise paint an empty list for a frame before snapping back.
  const totalPages = Math.max(1, Math.ceil(permissions.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  // important: every paged row carries its index into the full permissions array, never into the
  // visible page. The remove button edits that array, so a page-local index would delete the
  // wrong pending edit on any page after the first.
  const pagedPermissions = permissions
    .map((permission, index) => ({ permission, index }))
    .slice((currentPage - 1) * pageSize, currentPage * pageSize);

  if (!group) return null;

  const handleUpdateBasicInfo = async (e?: React.FormEvent | React.MouseEvent) => {
    if (e) e.preventDefault();

    try {
      await updateGroupMutation.mutateAsync({
        name: currentGroup?.name || group?.name || "",
        data: {
          description,
        },
        host: workingNode,
      });

      toast({
        title: "Group description updated",
      });
      
      // Give LXD a moment to persist the change before the refetch.
      await new Promise(resolve => setTimeout(resolve, 500));
      
      await refetchGroup();
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Could not update group description",
        description: error.message || "Try again.",
        variant: "destructive",
      });
    }
  };

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
        title: "Group permissions updated",
      });

      setIsPermissionsDirty(false);
      
      // Give LXD a moment to persist the change before the refetch.
      await new Promise(resolve => setTimeout(resolve, 500));
      
      const result = await refetchGroup();
      
      if (result.data) {
        const refreshedGroup = result.data as GroupInfo;
        setPermissions((refreshedGroup.permissions || []) as Permission[]);
      }
      
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Could not save group permissions",
        description: error.message || "Try again.",
        variant: "destructive",
      });
    }
  };

  const handleAddPermission = () => {
    if (newPermission.entity_type !== "server" && !allResources && selectedResources.length === 0) {
      toast({
        title: "Could not add permission",
        description: "Select at least one resource or choose 'All Resources'.",
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
      const uniquePerms = newPerms.filter(p => !isDuplicatePermission(p));
      const duplicateCount = newPerms.length - uniquePerms.length;
      
      if (uniquePerms.length === 0) {
        toast({
          title: "Duplicate permission",
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
        ? `Added ${plural(uniquePerms.length, "permission")}. ${plural(duplicateCount, "duplicate")} skipped.${viewNote}`
        : `Added ${plural(uniquePerms.length, "permission")}.${viewNote}`;
      
      toast({
        title: "Permission added",
        description: `${message} Click "Save Permissions" to apply.`,
      });
    }
  };

  const handleRemovePermission = (index: number) => {
    setPermissions(permissions.filter((_, i) => i !== index));
    setIsPermissionsDirty(true);
    toast({
      title: "Permission removed",
      description: `Permission removed. Click "Save Permissions" to apply changes.`,
    });
  };

  const handleAddIdentity = async () => {
    if (!newIdentity.identity_id.trim()) {
      toast({
        title: "Could not add identity to group",
        description: "Identity ID is required.",
        variant: "destructive",
      });
      return;
    }

    if (!currentGroup || !workingNode) {
      toast({
        title: "Could not add identity to group",
        description: `No valid cluster available. Current: ${currentGroup?.node || 'undefined'}, fallback: ${fallbackNode || 'none'}.`,
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
        title: "Identity added to group",
        description: `Identity added to group "${currentGroup.name}".`,
      });

      setNewIdentity({
        auth_type: "tls",
        identity_id: "",
      });

      await queryClient.invalidateQueries({ queryKey: ["groups"] });
      await queryClient.invalidateQueries({ queryKey: ["group"] });
      
      await refetchGroup();
      
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Could not add identity to group",
        description: error.message || "Try again.",
        variant: "destructive",
      });
    }
  };

  const confirmRemoveIdentity = (authType: string, identityId: string) => {
    setIdentityToRemove({ authType, id: identityId });
  };

  const handleRemoveIdentity = async () => {
    if (!identityToRemove) return;
    
    const { authType, id: identityId } = identityToRemove;
    
    if (!currentGroup || !workingNode) {
      toast({
        title: "Could not remove identity from group",
        description: "No valid cluster available.",
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
        title: "Identity removed from group",
        description: `Identity removed from group "${currentGroup.name}".`,
      });

      await queryClient.invalidateQueries({ queryKey: ["groups"] });
      await queryClient.invalidateQueries({ queryKey: ["group"] });
      
      await refetchGroup();
      
      onRefresh();
    } catch (error: any) {
      toast({
        title: "Could not remove identity from group",
        description: error.message || "Try again.",
        variant: "destructive",
      });
    } finally {
      setIdentityToRemove(null);
    }
  };

  const groupedIdentities: GroupedIdentities = {
    tls: currentGroup?.identities?.tls || [],
    oidc: currentGroup?.identities?.oidc || [],
  };

  if (!currentGroup) return null;

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
                  {updateGroupMutation.isPending ? "Saving…" : "Save Changes"}
                </Button>
              </DialogFooter>
            </form>
          </TabsContent>

          <TabsContent value="permissions" className="space-y-3 overflow-y-auto flex-1 min-h-0 pr-2">
            <div>
              <Label className="text-sm font-semibold">Group Permissions</Label>
              <p className="text-xs text-muted-foreground">
                Manage what resources this group can access
              </p>
            </div>

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

            <div className="border rounded-lg p-4 space-y-4">
              <Label className="text-sm font-medium">Add Permission</Label>
              
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

              {newPermission.entity_type !== "server" && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label className="text-xs text-muted-foreground">
                      2. Select Resources from {workingNode}
                    </Label>
                    {isLoadingResources() && (
                      <div className="flex items-center gap-1 text-xs text-muted-foreground">
                        <Loader2 className="h-3 w-3 animate-spin" />
                        <span>Loading…</span>
                      </div>
                    )}
                  </div>

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

                  {!allResources && (
                    <div className="space-y-2">
                      <div className="relative">
                        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                        <input
                          type="text"
                          placeholder={`Search ${newPermission.entity_type}s…`}
                          value={resourceSearchQuery}
                          onChange={(e) => setResourceSearchQuery(e.target.value)}
                          className="w-full h-9 pl-9 pr-3 text-sm border rounded-md bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                        />
                      </div>
                      <div className="border rounded-lg max-h-40 overflow-y-auto bg-background">
                        {isLoadingResources() ? (
                          <div className="p-4 text-center text-xs text-muted-foreground">
                            <Loader2 className="h-4 w-4 animate-spin mx-auto mb-1" />
                            Loading {newPermission.entity_type}s…
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
                      ✓ {plural(selectedResources.length, "resource")} selected
                    </div>
                  )}
                </div>
              )}

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
                <Badge variant="caution" className="mr-auto">
                  Unsaved changes
                </Badge>
              )}
              <Button
                onClick={handleSavePermissions}
                disabled={updateGroupMutation.isPending}
                variant={isPermissionsDirty ? "default" : "secondary"}
              >
                {updateGroupMutation.isPending ? "Saving…" : "Save Permissions"}
              </Button>
            </DialogFooter>
          </TabsContent>

          <TabsContent value="identities" className="space-y-3 overflow-y-auto flex-1 min-h-0 pr-2">
            <div>
              <Label className="text-base font-semibold">Group Members (Identities)</Label>
              <p className="text-sm text-muted-foreground mt-1">
                Add TLS certificates or OIDC users to this group
              </p>
            </div>

            <div className="space-y-2">
              <Label className="text-sm font-semibold">
                TLS Identities ({groupedIdentities.tls.length})
              </Label>
              {groupedIdentities.tls.length > 0 ? (
                <div className="space-y-2">
                  {groupedIdentities.tls.map((identityId, index) => {
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

            <div className="space-y-2">
              <Label className="text-sm font-semibold">
                OIDC Identities ({groupedIdentities.oidc.length})
              </Label>
              {groupedIdentities.oidc.length > 0 ? (
                <div className="space-y-2">
                  {groupedIdentities.oidc.map((identityId, index) => {
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

            <div className="border rounded-lg p-4 space-y-3">
              <Label className="text-sm font-medium">Add Identity</Label>
              
              {loadingIdentities ? (
                <div className="flex items-center justify-center py-6">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                  <span className="ml-2 text-sm text-muted-foreground">Loading identities…</span>
                </div>
              ) : (
                <>
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
                            <span className="text-muted-foreground">Search and select identity…</span>
                          )}
                          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                        </Button>
                      </PopoverTrigger>
                      <PopoverContent className="p-0" align="start" style={{ width: 'var(--radix-popover-trigger-width)' }}>
                        <Command shouldFilter={false}>
                          <CommandInput 
                            placeholder={`Search ${newIdentity.auth_type.toUpperCase()} identities…`}
                            value={identitySearchValue}
                            onValueChange={setIdentitySearchValue}
                          />
                          <CommandList>
                            <CommandEmpty>
                              {loadingIdentities ? "Loading identities…" : "No identities found."}
                            </CommandEmpty>
                            <CommandGroup>
                              {(allIdentities || [])
                                .filter(identity => {
                                  // LXD identity types: TLS is "Client certificate" (also
                                  // "Client certificate (pending)"), OIDC is "OIDC client".
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
                                  const tlsIdentities = currentGroup?.identities?.tls || [];
                                  const oidcIdentities = currentGroup?.identities?.oidc || [];
                                  const allGroupIdentities = [...tlsIdentities, ...oidcIdentities];
                                  return !allGroupIdentities.includes(identity.id);
                                })
                                .filter(identity => {
                                  // cmdk filtering is off; match on name and ID only.
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

                  <Button
                    type="button"
                    onClick={handleAddIdentity}
                    disabled={addIdentityMutation.isPending || !newIdentity.identity_id}
                    className="w-full"
                  >
                    {addIdentityMutation.isPending ? (
                      <>
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        Adding…
                      </>
                    ) : (
                      "Add Selected Identity"
                    )}
                  </Button>

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

      <Dialog open={showUnsavedWarning} onOpenChange={setShowUnsavedWarning}>
        <DialogContent className="max-w-md">
          <DialogHeader className="pb-4">
            <DialogTitle>
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
              {updateGroupMutation.isPending ? "Saving…" : "Save and Close"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!identityToRemove} onOpenChange={(open) => !open && setIdentityToRemove(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader className="pb-4">
            <DialogTitle className="text-destructive">
              Remove Identity
            </DialogTitle>
            <DialogDescription className="space-y-2">
              <span>Are you sure you want to remove this identity from the group?</span>
              {/* important: printed whole, not behind a reveal, so the operator sees exactly
                  which identity they are about to remove. */}
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
              {removeIdentityMutation.isPending ? "Removing…" : "Remove Identity"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Dialog>
  );
}
