"use client";

import { useEffect, useMemo, useState } from "react";
import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ClusterAccessSelector } from "@/components/cluster-access-selector";
import { ApiKeyCapabilitySelector } from "@/components/integrations/api-key-capability-selector";
import { formatApplicationType } from "@/lib/integration-display";
import type {
  APIKeyCreateRequest,
  ApplicationTypeOption,
  AvailableCluster,
  CapabilityOption,
  ClusterPermission,
} from "@/types";

const DEFAULT_APP_TYPE = "orcahub";

type ProjectScope = "default" | "all" | "specific";

function parseProjectList(raw: string): string[] {
  const names = raw
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  // An empty custom list would grant nothing and read as a bug rather than a choice. The
  // backend's own default for an unspecified project list is the narrow one, so match it.
  return names.length > 0 ? names : ["default"];
}

interface CreateAPIKeyDialogProps {
  clusters: AvailableCluster[];
  capabilities: CapabilityOption[];
  applicationTypes: ApplicationTypeOption[];
  // Resolves when the key is created and rejects when it is not, so the form is only cleared
  // once the work actually succeeded.
  onSubmit: (data: APIKeyCreateRequest) => Promise<unknown>;
  isSubmitting: boolean;
}

export function CreateAPIKeyDialog({
  clusters,
  capabilities,
  applicationTypes,
  onSubmit,
  isSubmitting,
}: CreateAPIKeyDialogProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [appName, setAppName] = useState("");
  const [appType, setAppType] = useState(DEFAULT_APP_TYPE);
  const [expireDays, setExpireDays] = useState<string>("never");
  const [selectedClusterIds, setSelectedClusterIds] = useState<string[]>([]);
  const [permissionLevel, setPermissionLevel] = useState("read");
  const [selectedCapabilities, setSelectedCapabilities] = useState<string[]>(
    [],
  );
  const [projectScope, setProjectScope] = useState<ProjectScope>("default");
  const [projectList, setProjectList] = useState("");

  const defaultsByType = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const option of applicationTypes ?? []) {
      if (option && typeof option.name === "string") {
        map.set(option.name, option.default_capabilities ?? []);
      }
    }
    return map;
  }, [applicationTypes]);

  // Seed the capability ticks from the chosen type. They stay editable afterwards: the preset is
  // a starting point, not a ceiling, so an operator can grant a custom application anything they
  // are willing to grant.
  const applyAppType = (nextType: string) => {
    setAppType(nextType);
    setSelectedCapabilities([...(defaultsByType.get(nextType) ?? [])]);
  };

  // The vocabulary arrives asynchronously, so seed the initial type once it lands rather than
  // leaving the form with no capabilities ticked.
  useEffect(() => {
    if (defaultsByType.size === 0) return;
    setSelectedCapabilities((current) =>
      current.length > 0 ? current : [...(defaultsByType.get(appType) ?? [])],
    );
  }, [defaultsByType, appType]);

  const resetForm = () => {
    setName("");
    setAppName("");
    setAppType(DEFAULT_APP_TYPE);
    setExpireDays("never");
    setSelectedClusterIds([]);
    setPermissionLevel("read");
    setSelectedCapabilities([...(defaultsByType.get(DEFAULT_APP_TYPE) ?? [])]);
    setProjectScope("default");
    setProjectList("");
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    const projects =
      projectScope === "all"
        ? ["*"]
        : projectScope === "specific"
          ? parseProjectList(projectList)
          : ["default"];

    const permissions: ClusterPermission[] = selectedClusterIds.map(
      (clusterId) => ({
        cluster_id: clusterId,
        permissions: [permissionLevel],
        projects,
      }),
    );

    try {
      await onSubmit({
        name,
        app_name: appName,
        app_type: appType,
        capabilities: selectedCapabilities,
        cluster_permissions: permissions,
        expire_days: expireDays === "never" ? null : parseInt(expireDays),
      });
    } catch {
      // The parent surfaces the error. Keep the dialog open with everything the operator typed:
      // clearing it on failure meant a rejected create silently discarded the whole form.
      return;
    }

    resetForm();
    setOpen(false);
  };

  const toggleCluster = (clusterId: string) => {
    setSelectedClusterIds((prev) =>
      prev.includes(clusterId)
        ? prev.filter((id) => id !== clusterId)
        : [...prev, clusterId],
    );
  };

  const toggleCapability = (capability: string) => {
    setSelectedCapabilities((prev) =>
      prev.includes(capability)
        ? prev.filter((entry) => entry !== capability)
        : [...prev, capability],
    );
  };

  const typeOptions = applicationTypes?.length
    ? applicationTypes
    : [{ name: DEFAULT_APP_TYPE, default_capabilities: [] }];

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">
          <Plus className="h-4 w-4 mr-2" />
          Create API Key
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="shrink-0">
          <DialogTitle>Create New API Key</DialogTitle>
          <DialogDescription>
            Generate an API key for external application integration.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
          <DialogBody className="-mr-2 pr-2">
            <div className="grid gap-4 py-4">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="name">Key Name</Label>
                  <Input
                    id="name"
                    placeholder="e.g., Production Orcahub"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    maxLength={100}
                    required
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="app_name">Application Name</Label>
                  <Input
                    id="app_name"
                    placeholder="e.g., Orcahub Production"
                    value={appName}
                    onChange={(e) => setAppName(e.target.value)}
                    maxLength={100}
                    required
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Application Type</Label>
                  <Select value={appType} onValueChange={applyAppType}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {typeOptions.map((option) => (
                        <SelectItem key={option.name} value={option.name}>
                          {formatApplicationType(option.name)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>Expiration</Label>
                  <Select value={expireDays} onValueChange={setExpireDays}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="never">Never expires</SelectItem>
                      <SelectItem value="30">30 days</SelectItem>
                      <SelectItem value="90">90 days</SelectItem>
                      <SelectItem value="365">1 year</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <ApiKeyCapabilitySelector
                capabilities={capabilities}
                selected={selectedCapabilities}
                onToggle={toggleCapability}
              />

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label>Permission Level</Label>
                  <Select
                    value={permissionLevel}
                    onValueChange={setPermissionLevel}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="read">Read Only</SelectItem>
                      <SelectItem value="write">Read &amp; Write</SelectItem>
                      <SelectItem value="admin">Full Admin</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>Project Scope</Label>
                  <Select
                    value={projectScope}
                    onValueChange={(value) =>
                      setProjectScope(value as ProjectScope)
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="default">
                        Default project only
                      </SelectItem>
                      <SelectItem value="all">All projects</SelectItem>
                      <SelectItem value="specific">
                        Specific projects
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              {projectScope === "specific" && (
                <div className="space-y-2">
                  <Label htmlFor="project_list">Project Names</Label>
                  <Input
                    id="project_list"
                    placeholder="e.g., default, staging, tenant-a"
                    value={projectList}
                    onChange={(e) => setProjectList(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    Comma separated. Applies to every selected cluster. Leave
                    empty to fall back to the default project.
                  </p>
                </div>
              )}

              <ClusterAccessSelector
                clusters={clusters}
                selectedClusterIds={selectedClusterIds}
                onToggle={toggleCluster}
              />
            </div>
          </DialogBody>
          <DialogFooter className="shrink-0 border-t pt-3">
            <Button
              type="button"
              variant="outline"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                isSubmitting ||
                !name ||
                !appName ||
                selectedClusterIds.length === 0 ||
                selectedCapabilities.length === 0
              }
            >
              {isSubmitting ? "Creating..." : "Create API Key"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
