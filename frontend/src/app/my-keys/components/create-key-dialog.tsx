"use client";

import { formatDate } from "@/lib/utils";
import { useState } from "react";
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

// Generate Key Dialog Component
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
    node: "",  // Don't auto-select - let user choose
    project: "",  // Don't auto-select - let user choose
    auth_level: "standard",
    expire_days: 365 });

  // Get unique projects for the selected node
  const filteredProjects = formData.node 
    ? projects.filter((p: any) => !p.host || p.host === formData.node)
    : [];
  
  // Get unique project names to avoid duplicates
  const uniqueProjects = filteredProjects.reduce((acc: any[], project: any) => {
    if (!acc.find(p => p.name === project.name)) {
      acc.push(project);
    }
    return acc;
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.node) {
      return; // Don't submit without node
    }
    if (formData.auth_level === "standard" && !formData.project) {
      return; // Don't submit without project for standard auth
    }
    onSubmit(formData);
  };

  // Reset project when node changes
  const handleNodeChange = (value: string) => {
    setFormData({ ...formData, node: value, project: "" });
  };

  return (
    <DialogContent className="sm:max-w-[550px]">
      <DialogHeader>
        <DialogTitle>Generate New Key</DialogTitle>
        <DialogDescription>
          Generate a new TLS identity key for accessing hypervisor resources. The key will
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
              {formData.auth_level === "standard" ? (
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
                // Set to 365 if empty on blur
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
          <Button type="submit" disabled={isSubmitting}>
            {isSubmitting ? "Generating…" : "Generate Key"}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}
