"use client";

import { useEffect, useMemo, useRef, useState } from "react";
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
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SearchInput } from "@/components/ui/search-input";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PANEL_PAGE_SIZE } from "@/lib/pagination";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Plus, X, Loader2, Info } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { getResourceBasePath, getValidEntitlements, getEntitlementLabel } from "@/lib/lxd-entitlements";
import { OverflowText } from "@/components/ui/overflow-text";
import { 
  useCreateGroup, 
  useProjects, 
  useInstances, 
  useImageStores, 
  useProfiles, 
  useNetworks, 
  useStoragePools 
} from "@/hooks/use-lxd";

interface CreateGroupDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Reported after the dialog closes, so the list can bring the new row into view. */
  onSaved?: (created: { node: string; name: string }) => void;
}

interface Permission {
  entity_type: string;
  url: string;
  entitlement: string;
}

type Resource = PermissionResource;

/** A checkbox list inside a 64-unit scroller. Ten rows fills it without hiding the pager. */
const RESOURCE_PAGE_SIZE = PANEL_PAGE_SIZE;

export function CreateGroupDialog({ open, onOpenChange, onSaved }: CreateGroupDialogProps) {
  const { toast } = useToast();
  const createGroupMutation = useCreateGroup();
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  // The list is ordered reachable-first, so the first selectable option is a cluster that can
  // actually take a group. This used to default to nodes[0] of a list the page had already
  // filtered down to whatever answered, which hid the rest rather than ranking them.
  const defaultNode = useMemo(
    () => nodeOptions.find((o) => !o.disabled)?.value ?? "",
    [nodeOptions],
  );

  const [formData, setFormData] = useState({
    name: "",
    description: "",
    node: "",
  });

  // note: reachability arrives after the dialog can open, so the default cannot be a useState
  // initialiser. Only ever fills an empty choice, never overwrites the operator's.
  useEffect(() => {
    if (!open || !defaultNode) return;
    setFormData((prev) => (prev.node ? prev : { ...prev, node: defaultNode }));
  }, [open, defaultNode]);
  
  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [newPermission, setNewPermission] = useState({
    entity_type: "project",
    entitlement: "can_view",
  });
  
  const [selectedResources, setSelectedResources] = useState<string[]>([]);
  const [allResources, setAllResources] = useState(false);
  const [resourceSearch, setResourceSearch] = useState("");
  const [resourcePage, setResourcePage] = useState(1);
  
  // Fetch resources based on selected node
  // Gated on a node: the dialog mounts closed and used to read (and poll) the whole fleet's
  // instances, images, profiles, networks and pools before the operator had picked anything.
  const hasNode = !!formData.node;
  const { data: projectsData, isLoading: loadingProjects } = useProjects(formData.node);
  const { data: instancesData, isLoading: loadingInstances } = useInstances(formData.node, { enabled: hasNode });
  // Every image store on the node, each row naming its store; a group permission names an
  // image in the store that holds it.
  const { data: imagesData, isLoading: loadingImages } = useImageStores(formData.node, { enabled: hasNode });
  const { data: profilesData, isLoading: loadingProfiles } = useProfiles(formData.node, undefined, { enabled: hasNode });
  const { data: networksData, isLoading: loadingNetworks } = useNetworks(formData.node, undefined, { enabled: hasNode });
  const { data: poolsData, isLoading: loadingPools } = useStoragePools(formData.node, { enabled: hasNode });

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
    // Get valid entitlements for new entity type
    const validEntitlements = getValidEntitlements(value);
    // Reset to first valid entitlement
    const defaultEntitlement = validEntitlements[0] || "can_view";
    
    setNewPermission({ 
      entity_type: value, 
      entitlement: defaultEntitlement 
    });
    setSelectedResources([]);
    setAllResources(false);
  };

  const handleResourceToggle = (resourceName: string) => {
    setSelectedResources(prev => 
      prev.includes(resourceName) 
        ? prev.filter(r => r !== resourceName)
        : [...prev, resourceName]
    );
  };

  // The list under the checkbox is live LXD data: every image pulled or cached on the node, every
  // instance created on it. It rendered all of them into a 64-unit scroller, so a node with a
  // cached image catalogue put thousands of checkboxes in the DOM with no way to find one.
  //
  // important: selection is stored by resource name, not by position, so paging and searching
  // cannot lose a tick that is off screen. handleAddPermission still reads the full list.
  const availableResources = getAvailableResources();
  const matchingResources = resourceSearch.trim()
    ? availableResources.filter((r) =>
        r.name.toLowerCase().includes(resourceSearch.trim().toLowerCase()),
      )
    : availableResources;
  const resourcePages = Math.max(1, Math.ceil(matchingResources.length / RESOURCE_PAGE_SIZE));
  const currentResourcePage = Math.min(resourcePage, resourcePages);
  const pagedResources = matchingResources.slice(
    (currentResourcePage - 1) * RESOURCE_PAGE_SIZE,
    currentResourcePage * RESOURCE_PAGE_SIZE,
  );

  // Compared during render rather than in an effect: switching the resource type or the node
  // replaces the list entirely, and an effect would paint the old page against the new data.
  const resourceListKey = `${formData.node}:${newPermission.entity_type}:${resourceSearch}`;
  const [lastResourceListKey, setLastResourceListKey] = useState(resourceListKey);
  if (lastResourceListKey !== resourceListKey) {
    setLastResourceListKey(resourceListKey);
    setResourcePage(1);
  }

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
      // important: keyed by path, not name. Every LXD project carries its own `default`
      // profile, so a bare name matches the first row of that name whatever project it is in,
      // and the grant lands on the wrong project's resource. The edit dialog already keys by
      // path; this one was left behind.
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

    const prerequisites = projectViewPrerequisites(newPerms, permissions);
    setPermissions([...permissions, ...newPerms, ...prerequisites]);
    setSelectedResources([]);
    setAllResources(false);
    if (prerequisites.length > 0) {
      toast({
        title: "Project view added",
        description: `LXD grants a resource inside a project only to a group that can view the project, so can_view was added on ${projectNamesOf(prerequisites).join(", ")}.`,
      });
    }
  };

  const handleRemovePermission = (index: number) => {
    setPermissions(permissions.filter((_, i) => i !== index));
  };

  // important: isPending is React state, so it only reaches the button's disabled prop on the
  // next render. Two clicks inside one tick both pass it and both create a group. Claimed after
  // the validation returns below, which are synchronous and cheap, so they need no release.
  const submitInFlightRef = useRef(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    
    if (!formData.name.trim()) {
      toast({
        title: "Validation Error",
        description: "Group name is required",
        variant: "destructive",
      });
      return;
    }

    if (!formData.node) {
      toast({
        title: "Validation Error",
        description: "Please select a node",
        variant: "destructive",
      });
      return;
    }

    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;

    try {
      await createGroupMutation.mutateAsync({
        data: {
          name: formData.name,
          description: formData.description,
          permissions: permissions,
        },
        host: formData.node,
      });

      toast({
        title: "Success",
        description: `Group "${formData.name}" created successfully`,
      });

      const created = { node: formData.node, name: formData.name };
      // Reset form
      setFormData({ name: "", description: "", node: defaultNode });
      setPermissions([]);
      setNewPermission({ entity_type: "project", entitlement: "can_view" });
      setSelectedResources([]);
      setAllResources(false);
      onOpenChange(false);
      onSaved?.(created);
    } catch (error: any) {
      toast({
        title: "Error",
        description: error.message || "Failed to create group",
        variant: "destructive",
      });
    } finally {
      submitInFlightRef.current = false;
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-4xl max-h-[85vh] overflow-y-auto p-6">
        <DialogHeader className="pb-4">
          <DialogTitle>Create New Group</DialogTitle>
          <DialogDescription>
            Create a new authentication group with custom permissions
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-6">
          {/* Basic Information */}
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="node">Target Node *</Label>
              <SearchableSelect
                id="node"
                value={formData.node}
                onValueChange={(value) => setFormData({ ...formData, node: value })}
                options={nodeOptions}
                placeholder={loadingNodes ? "Loading..." : "Select a node"}
                searchPlaceholder="Search nodes..."
                emptyText="No nodes available."
                disabled={loadingNodes}
              />
              <NodeStatusNotice
                meta={formData.node ? nodePicker.statusOf(formData.node) : undefined}
                reason={
                  formData.node
                    ? nodePicker.availabilityOf(formData.node)?.reason ?? undefined
                    : undefined
                }
                nodeLabel={formData.node ? nodePicker.labelOf(formData.node) : undefined}
                unavailable={nodePicker.statusUnavailable}
                noSelectableNodes={nodePicker.selectableCount === 0}
                updatedAt={nodePicker.statusUpdatedAt}
                onRecheck={nodePicker.refreshNow}
              />
              <p className="text-xs text-muted-foreground">
                Resources will be fetched from this node
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="name">Group Name *</Label>
              <Input
                id="name"
                placeholder="e.g., developers, readonly-users"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="description">Description</Label>
              <Textarea
                id="description"
                placeholder="Brief description of this group's purpose"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                rows={3}
              />
            </div>
          </div>

          {/* Permissions Section */}
          <div className="space-y-4">
            <div className="flex items-center gap-2">
              <Label className="text-base font-semibold">Permissions</Label>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Info className="h-4 w-4 text-muted-foreground cursor-help" />
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs">
                    <p className="text-xs">
                      Select resources from your node and set permission levels. 
                      Resources are automatically fetched from {formData.node}.
                    </p>
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>

            {/* Existing Permissions */}
            {permissions.length > 0 && (
              <div className="space-y-2 max-h-48 overflow-y-auto border rounded-lg p-3 bg-muted/30">
                <Label className="text-xs font-semibold text-muted-foreground">
                  Added Permissions ({permissions.length})
                </Label>
                {permissions.map((permission, index) => (
                  <div
                    key={index}
                    className="flex items-center gap-2 p-2 bg-background rounded-lg border"
                  >
                    <Badge variant="outline" className="text-xs">
                      {permission.entity_type}
                    </Badge>
                    {/* important: the URL is the identity of the permission being granted, and
                        it was clipped inside the flow that grants it. */}
                    <span className="min-w-0 flex-1 px-2 text-xs">
                      <OverflowText
                        value={permission.url}
                        label={`target for the ${permission.entity_type} permission`}
                        mono
                      />
                    </span>
                    <Badge variant="secondary" className="text-xs">
                      {permission.entitlement.replace('can_', '')}
                    </Badge>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => handleRemovePermission(index)}
                    >
                      <X className="h-3 w-3" />
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {/* Add New Permission */}
            <div className="border rounded-lg p-4 space-y-4 bg-card">
              <Label className="text-sm font-semibold">Add Permission</Label>
              
              {/* Step 1: Resource Type */}
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
                      2. Select Resources from {formData.node}
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
                      id="all-resources"
                      checked={allResources}
                      onCheckedChange={(checked) => {
                        const isChecked = checked === true;
                        setAllResources(isChecked);
                        if (isChecked) setSelectedResources([]);
                      }}
                      className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0"
                    />
                    <Label
                      htmlFor="all-resources"
                      className="text-xs font-medium cursor-pointer flex-1"
                    >
                      All {newPermission.entity_type}s (*)
                    </Label>
                  </div>

                  {/* Resource List */}
                  {!allResources && (
                    <>
                    <SearchInput
                      value={resourceSearch}
                      onChange={setResourceSearch}
                      placeholder={`Search ${newPermission.entity_type}s`}
                      label={`Search ${newPermission.entity_type}s on ${formData.node}`}
                      clearLabel={`Clear ${newPermission.entity_type} search`}
                      className="mb-2"
                    />
                    <div className="border rounded-lg max-h-64 overflow-y-auto bg-background">
                      {isLoadingResources() ? (
                        <div className="p-6 text-center text-sm text-muted-foreground">
                          <Loader2 className="h-6 w-6 animate-spin mx-auto mb-2" />
                          Loading {newPermission.entity_type}s from {formData.node}...
                        </div>
                      ) : availableResources.length === 0 ? (
                        <div className="p-6 text-center text-sm text-muted-foreground">
                          No {newPermission.entity_type}s found on {formData.node}
                        </div>
                      ) : matchingResources.length === 0 ? (
                        <div className="p-6 text-center text-sm text-muted-foreground">
                          No {newPermission.entity_type} matches that search. There are{" "}
                          {availableResources.length} on {formData.node}.
                        </div>
                      ) : (
                        <div className="p-2 space-y-1">
                          {pagedResources.map((resource) => (
                            <div
                              key={resource.path}
                              className="flex items-center gap-2 p-1.5 hover:bg-accent rounded transition-colors"
                            >
                              <Checkbox
                                id={`resource-${resource.path}`}
                                checked={selectedResources.includes(resource.path)}
                                onCheckedChange={(checked) => {
                                  if (checked === true || checked === false) {
                                    handleResourceToggle(resource.path);
                                  }
                                }}
                                className="h-3.5 w-3.5 sm:h-4 sm:w-4 shrink-0"
                              />
                              <Label 
                                htmlFor={`resource-${resource.path}`}
                                className="text-xs cursor-pointer font-medium flex-1 break-words"
                              >
                                {resource.name}
                                {resourceQualifier(newPermission.entity_type, resource.project) && (
                                  <span className="ml-1 text-xs text-muted-foreground">
                                    ({resourceQualifier(newPermission.entity_type, resource.project)})
                                  </span>
                                )}
                              </Label>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                    </>
                  )}

                  {newPermission.entity_type === "image" && imagesData?.complete === false && (
                    <p className="text-xs text-muted-foreground">
                      Some image stores on {formData.node} could not be read, so this list may be short.
                    </p>
                  )}

                  {/* Absent rather than disabled when every resource fits on one page. */}
                  {!allResources && resourcePages > 1 && (
                    <DataTablePagination
                      variant="compact"
                      label={`${newPermission.entity_type}s on ${formData.node}`}
                      currentPage={currentResourcePage}
                      totalPages={resourcePages}
                      onPageChange={setResourcePage}
                    />
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
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={createGroupMutation.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={createGroupMutation.isPending}>
              {createGroupMutation.isPending && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {createGroupMutation.isPending ? "Creating..." : "Create Group"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
