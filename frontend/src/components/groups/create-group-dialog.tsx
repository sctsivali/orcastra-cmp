"use client";

import { useRef, useState } from "react";
import { lxdResourcePath } from "@/lib/lxd-resource-path";
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
  useImages, 
  useProfiles, 
  useNetworks, 
  useStoragePools 
} from "@/hooks/use-lxd";

interface CreateGroupDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  nodes: string[];
}

interface Permission {
  entity_type: string;
  url: string;
  entitlement: string;
}

interface Resource {
  name: string;
  path: string;
  // the project that holds it, so two rows both called "default" can be told apart
  project?: string;
}

export function CreateGroupDialog({ open, onOpenChange, nodes }: CreateGroupDialogProps) {
  const { toast } = useToast();
  const createGroupMutation = useCreateGroup();
  
  const [formData, setFormData] = useState({
    name: "",
    description: "",
    node: nodes[0] || "",
  });
  
  const [permissions, setPermissions] = useState<Permission[]>([]);
  const [newPermission, setNewPermission] = useState({
    entity_type: "project",
    entitlement: "can_view",
  });
  
  const [selectedResources, setSelectedResources] = useState<string[]>([]);
  const [allResources, setAllResources] = useState(false);
  
  // Fetch resources based on selected node
  const { data: projectsData, isLoading: loadingProjects } = useProjects(formData.node);
  const { data: instancesData, isLoading: loadingInstances } = useInstances(formData.node);
  const { data: imagesData, isLoading: loadingImages } = useImages(formData.node);
  const { data: profilesData, isLoading: loadingProfiles } = useProfiles(formData.node);
  const { data: networksData, isLoading: loadingNetworks } = useNetworks(formData.node);
  const { data: poolsData, isLoading: loadingPools } = useStoragePools(formData.node);

  // Get available resources based on entity type
  const getAvailableResources = (): Resource[] => {
    switch (newPermission.entity_type) {
      case "project":
        return ((projectsData as any)?.projects || []).map((p: any) => ({ 
          name: p.name, 
          path: `/1.0/projects/${p.name}` 
        }));
      case "instance":
        return ((instancesData as any)?.vms || []).map((i: any) => ({ 
          name: i.name, 
          path: `/1.0/instances/${i.name}` 
        }));
      case "image":
        // Handle both array of hosts and direct images array
        const images = Array.isArray(imagesData) 
          ? imagesData.flatMap((host: any) => host?.images || [])
          : (imagesData as any)?.images || [];
        return images.map((img: any) => ({
          name:
            img.description ||
            img.properties?.description ||
            img.aliases?.find(Boolean) ||
            img.fingerprint ||
            EMPTY_VALUE,
          path: `/1.0/images/${img.fingerprint}`
        }));
      case "profile":
        return ((profilesData as any)?.profiles || []).map((p: any) => ({
          name: p.name,
          path: lxdResourcePath("profiles", p.name, p.project),
          project: p.project,
        }));
      case "network":
        return ((networksData as any)?.networks || []).map((n: any) => ({
          name: n.name,
          path: lxdResourcePath("networks", n.name, n.project),
          project: n.project,
        }));
      case "storage_pool":
        return ((poolsData as any)?.pools || []).map((s: any) => ({ 
          name: s.name, 
          path: `/1.0/storage-pools/${s.name}` 
        }));
      default:
        return [];
    }
  };

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

    setPermissions([...permissions, ...newPerms]);
    setSelectedResources([]);
    setAllResources(false);
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

      // Reset form
      setFormData({ name: "", description: "", node: nodes[0] || "" });
      setPermissions([]);
      setNewPermission({ entity_type: "project", entitlement: "can_view" });
      setSelectedResources([]);
      setAllResources(false);
      onOpenChange(false);
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
        <DialogHeader>
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
              <Select
                value={formData.node}
                onValueChange={(value) => setFormData({ ...formData, node: value })}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select a node" />
                </SelectTrigger>
                <SelectContent>
                  {nodes.map((node) => (
                    <SelectItem key={node} value={node}>
                      {node}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
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
                    <div className="border rounded-lg max-h-64 overflow-y-auto bg-background">
                      {isLoadingResources() ? (
                        <div className="p-6 text-center text-sm text-muted-foreground">
                          <Loader2 className="h-6 w-6 animate-spin mx-auto mb-2" />
                          Loading {newPermission.entity_type}s from {formData.node}...
                        </div>
                      ) : getAvailableResources().length === 0 ? (
                        <div className="p-6 text-center text-sm text-muted-foreground">
                          No {newPermission.entity_type}s found on {formData.node}
                        </div>
                      ) : (
                        <div className="p-2 space-y-1">
                          {getAvailableResources().map((resource) => (
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
                                {resource.project && resource.project !== "default" && (
                                  <span className="ml-1 text-xs text-muted-foreground">
                                    ({resource.project})
                                  </span>
                                )}
                              </Label>
                            </div>
                          ))}
                        </div>
                      )}
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
