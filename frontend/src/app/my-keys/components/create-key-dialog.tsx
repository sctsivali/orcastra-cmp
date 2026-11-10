"use client";

import { formatDate } from "@/lib/utils";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle } from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { KeyCreateRequest } from "@/types/api";

export function CreateKeyDialog({
  projects,
  projectsLoading,
  onSubmit,
  isSubmitting }: {
  projects: any[];
  projectsLoading: boolean;
  onSubmit: (data: KeyCreateRequest) => void;
  isSubmitting: boolean;
}) {
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;

  const [formData, setFormData] = useState<KeyCreateRequest>({
    name: "",
    node: "",  // Nothing is preselected: the user chooses.
    project: "",
    auth_level: "standard",
    expire_days: 365 });

  // Which engine the key would be issued on, and whether the cluster's build allows one at all.
  const capability = useQuery({
    queryKey: ["my-keys", "capability", formData.node],
    queryFn: () => api.myKeys.capability(formData.node),
    enabled: !!formData.node,
    staleTime: 60_000,
    retry: false,
  });
  const engine = capability.data?.engine ?? null;
  const refused = capability.data ? !capability.data.allowed : false;

  // Projects on the selected cluster; a project with no host is offered on every cluster.
  const filteredProjects = formData.node 
    ? projects.filter((p: any) => !p.host || p.host === formData.node)
    : [];
  
  // One entry per project name.
  const uniqueProjects = filteredProjects.reduce((acc: any[], project: any) => {
    if (!acc.find(p => p.name === project.name)) {
      acc.push(project);
    }
    return acc;
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.node) {
      return;
    }
    if (formData.auth_level === "standard" && !formData.project) {
      return;
    }
    if (refused) {
      return; // The capability notice says why
    }
    onSubmit(formData);
  };

  // A project belongs to one cluster, so changing the cluster clears it.
  const handleNodeChange = (value: string) => {
    setFormData({ ...formData, node: value, project: "" });
  };

  return (
    <DialogContent className="sm:max-w-[550px]">
      <DialogHeader>
        <DialogTitle>Generate New Key</DialogTitle>
        <DialogDescription>
          Generate a new TLS client key for accessing hypervisor resources. The key will
          be generated with specific permissions for the selected project.
        </DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit}>
        <div className="grid gap-4 py-4">
          <div className="grid gap-2">
            <Label htmlFor="name">Key Name</Label>
            <Input
              id="name"
              placeholder="for example, Production Access Key"
              value={formData.name}
              onChange={(e) =>
                setFormData({ ...formData, name: e.target.value })
              }
              required
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="node">Cluster</Label>
            <SearchableSelect
              id="node"
              value={formData.node}
              onValueChange={handleNodeChange}
              options={nodeOptions}
              placeholder={loadingNodes ? "Loading…" : "Select a cluster"}
              searchPlaceholder="Search clusters…"
              emptyText="No clusters available."
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
            {capability.data && !capability.data.allowed && capability.data.reason && (
              <Alert variant="destructive" data-key-capability="refused">
                <ShieldAlert className="h-4 w-4" />
                <AlertDescription>{capability.data.reason}</AlertDescription>
              </Alert>
            )}
          </div>
          {formData.auth_level === "standard" && (
            <div className="grid gap-2">
              <Label htmlFor="project">Project</Label>
              <SearchableSelect
                id="project"
                value={formData.project}
                onValueChange={(value) =>
                  setFormData({ ...formData, project: value })
                }
                options={uniqueProjects.map((project: any) => ({
                  value: project.name,
                  label: project.name,
                }))}
                placeholder={
                  !formData.node
                    ? "Select a cluster first"
                    : projectsLoading
                      ? "Loading projects…"
                      : uniqueProjects.length === 0
                        ? `No projects available for ${formData.node}`
                        : "Select a project"
                }
                searchPlaceholder="Search projects…"
                emptyText="No projects found."
                disabled={projectsLoading || !formData.node || uniqueProjects.length === 0}
              />
            </div>
          )}
          <div className="grid gap-2">
            <Label htmlFor="auth_level">Authorization Level</Label>
            <Select
              value={formData.auth_level}
              onValueChange={(value) =>
                setFormData({ ...formData, auth_level: value })
              }
            >
              <SelectTrigger id="auth_level">
                <SelectValue placeholder="Select access level" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="standard">Standard</SelectItem>
                <SelectItem value="admin">Admin</SelectItem>
              </SelectContent>
            </Select>
            <div className="text-xs text-muted-foreground space-y-1">
              {engine === "incus" ? (
                formData.auth_level === "standard" ? (
                  <p>
                    Project certificate: full control of instances, storage and networks in the selected
                    project, including shell, console and file access. No other projects and no server
                    configuration.
                  </p>
                ) : (
                  <p>Unrestricted certificate: full administrator of the cluster.</p>
                )
              ) : formData.auth_level === "standard" ? (
                <p>Project-level access: manage instances and resources within the selected project only.</p>
              ) : (
                <p>Cluster-level access: full administrative control across all projects and the cluster&apos;s server configuration.</p>
              )}
            </div>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="expire_days">
              Expiration (Days)
            </Label>
            <Input
              id="expire_days"
              type="number"
              min={1}
              max={3650}
              value={formData.expire_days}
              onChange={(e) => {
                const value = e.target.value;
                // Allow empty string for clearing
                if (value === '') {
                  setFormData({
                    ...formData,
                    expire_days: '' as any });
                } else {
                  const numValue = parseInt(value);
                  if (!isNaN(numValue) && numValue >= 1 && numValue <= 3650) {
                    setFormData({
                      ...formData,
                      expire_days: numValue });
                  }
                }
              }}
              onBlur={(e) => {
                if (e.target.value === '' || parseInt(e.target.value) < 1) {
                  setFormData({
                    ...formData,
                    expire_days: 365 });
                }
              }}
              placeholder="365"
              required
            />
            <p className="text-xs text-muted-foreground">
              Key will expire on{" "}
              {formatDate(new Date(Date.now() + (formData.expire_days || 365) * 86_400_000).toISOString())}
            </p>
          </div>
        </div>
        <DialogFooter>
          <Button type="submit" disabled={isSubmitting || refused}>
            {isSubmitting ? "Generating…" : "Generate Key"}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
