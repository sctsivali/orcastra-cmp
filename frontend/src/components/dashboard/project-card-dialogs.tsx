"use client";

import * as React from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle } from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue } from "@/components/ui/select";
import { useDeleteProject, useUpdateProject } from "@/hooks/use-lxd";
import { toast } from "@/hooks/use-toast";
import {
  editableLxdLimit,
  editableStorageLimit,
  type LxdLimitUnit,
  type StorageLimitUnit,
} from "@/lib/lxd-limits";
import { ProjectInfo } from "@/types";

interface ProjectDialogProps {
  project: ProjectInfo;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  isReadOnly: boolean;
}

/**
 * The project card's delete confirm. Mounted for as long as the card is, so Radix keeps its
 * exit fade; the card owns the open flag because its menu sets it.
 */
export function ProjectDeleteDialog({ project, instanceCount, open, onOpenChange, isReadOnly }: ProjectDialogProps & { instanceCount: number }) {
  const deleteProject = useDeleteProject();
  const [isDeleting, setIsDeleting] = React.useState(false);

  const handleDelete = async (e: React.MouseEvent) => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Project deletion is disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    e.stopPropagation();
    setIsDeleting(true);
    try {
      await deleteProject.mutateAsync({
        host: project.host,
        projectName: project.name });
      toast({
        title: "Project Deleted",
        description: `Project '${project.name}' has been deleted from ${project.host}.`,
        variant: "success" });
      onOpenChange(false);
    } catch (error) {
      toast({
        title: "Failed to delete project",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
    <AlertDialogContent onClick={(e) => e.stopPropagation()}>
      <AlertDialogHeader>
        <AlertDialogTitle>Delete Project &quot;{project.name}&quot;?</AlertDialogTitle>
        <AlertDialogDescription>
          This will permanently delete the project from {project.host}. 
          {instanceCount > 0 && (
            <span className="block mt-2 text-destructive font-medium">
              Warning: This project has {instanceCount} instance(s). Delete all instances first.
            </span>
          )}
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>Cancel</AlertDialogCancel>
        <AlertDialogAction
          onClick={handleDelete}
          disabled={isDeleting || instanceCount > 0}
          className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
        >
          {isDeleting ? (
            <>
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              Deleting...
            </>
          ) : (
            "Delete Project"
          )}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
  );
}

/** The project card's edit form. The fields are re-read from the project each time it opens. */
export function ProjectEditDialog({ project, open, onOpenChange, refetch, isReadOnly }: ProjectDialogProps & { refetch: () => void }) {
  const updateProject = useUpdateProject();
  const [isSaving, setIsSaving] = React.useState(false);
  const [editName, setEditName] = React.useState(project.name);
  const [editDescription, setEditDescription] = React.useState(project.description || "");
  const [editMaxCpu, setEditMaxCpu] = React.useState(0);
  const [editMaxRamValue, setEditMaxRamValue] = React.useState(0);
  const [editMaxRamUnit, setEditMaxRamUnit] = React.useState<LxdLimitUnit>("GiB");
  const [editMaxRamSource, setEditMaxRamSource] = React.useState<string | null>(null);
  const [ramLimitDirty, setRamLimitDirty] = React.useState(false);
  const [editMaxStorageValue, setEditMaxStorageValue] = React.useState(0);
  const [editMaxStorageUnit, setEditMaxStorageUnit] = React.useState<StorageLimitUnit>("GB");
  const [editMaxStorageSource, setEditMaxStorageSource] = React.useState<string | null>(null);
  const [storageLimitDirty, setStorageLimitDirty] = React.useState(false);

  // Parse existing resource limits when dialog opens
  React.useEffect(() => {
    if (open) {
      const config = project.config ?? {};
      // Set name and description
      setEditName(project.name);
      setEditDescription(project.description || "");

      // Parse CPU limit (0 = unlimited/no limit set)
      const cpuLimitStr = config["limits.cpu"];
      setEditMaxCpu(cpuLimitStr ? parseInt(cpuLimitStr) || 0 : 0);

      const ram = editableLxdLimit(config["limits.memory"]);
      setEditMaxRamValue(ram.value);
      setEditMaxRamUnit(ram.unit);
      setEditMaxRamSource(ram.preservedRaw);
      setRamLimitDirty(false);

      const storage = editableStorageLimit(config["limits.disk"]);
      setEditMaxStorageValue(storage.value);
      setEditMaxStorageUnit(storage.unit);
      setEditMaxStorageSource(storage.preservedRaw);
      setStorageLimitDirty(false);
    }
  }, [open, project.config, project.description, project.name]);

  // important: isPending is React state, so the disabled prop only updates on the next render.
  // Two clicks inside one tick both fire the mutation. Claimed synchronously, before any await.
  const submitInFlightRef = React.useRef(false);

  const handleSave = async () => {
    if (isReadOnly) {
      toast({
        title: "Read-Only Mode Active",
        description: "Project updates are disabled in read-only mode",
        variant: "destructive" });
      return;
    }
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setIsSaving(true);
    try {
      const updateData: {
        description?: string;
        max_cpu?: number;
        max_ram?: string;
        max_storage?: string;
      } = {};

      // Always include description
      updateData.description = editDescription;

      // CPU: 0 = unlimited (send 0 to clear limit), >0 = set limit
      // null/undefined = no change (field omitted)
      updateData.max_cpu = editMaxCpu; // always send - 0 clears the limit

      // Omit untouched limits so a valid legacy SI/raw-byte value keeps its exact meaning.
      if (ramLimitDirty) {
        updateData.max_ram = editMaxRamValue > 0
          ? `${editMaxRamValue}${editMaxRamUnit}`
          : "0";
      }
      if (storageLimitDirty) {
        updateData.max_storage = editMaxStorageValue > 0
          ? `${editMaxStorageValue}${editMaxStorageUnit}`
          : "0";
      }

      await updateProject.mutateAsync({ host: project.host, projectName: project.name, data: updateData });

      toast({
        title: "Project Updated",
        description: `Project '${project.name}' has been updated successfully.`,
        variant: "success" });

      onOpenChange(false);
      refetch(); // Refresh the project list
    } catch (error) {
      toast({
        title: "Failed to update project",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive" });
    } finally {
      submitInFlightRef.current = false;
      setIsSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Edit Project</DialogTitle>
          <DialogDescription>
            Update the name, description and resource limits for {project.name}
          </DialogDescription>
        </DialogHeader>
        
        <div className="space-y-4 py-4">
          {/* Project Name */}
          <div className="space-y-2">
            <Label htmlFor="edit-name">Project Name</Label>
            <Input
              id="edit-name"
              placeholder="Project name"
              value={editName}
              disabled
              className="bg-muted cursor-not-allowed"
            />
            <p className="text-xs text-muted-foreground">
              Note: Renaming projects is not supported by the current hypervisor API.
            </p>
          </div>

          {/* Description */}
          <div className="space-y-2">
            <Label htmlFor="edit-description">Description</Label>
            <Textarea
              id="edit-description"
              placeholder="Project description"
              value={editDescription}
              onChange={(e) => setEditDescription(e.target.value)}
              rows={3}
            />
          </div>

          {/* Max CPU */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="edit-max-cpu">Max vCPUs</Label>
              {editMaxCpu === 0 && <span className="text-xs text-muted-foreground">Unlimited</span>}
            </div>
            <Input
              id="edit-max-cpu"
              type="number"
              min="0"
              value={editMaxCpu}
              onChange={(e) => setEditMaxCpu(Number(e.target.value))}
              placeholder="0 = Unlimited"
            />
          </div>

          {/* Max Memory */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="edit-max-ram">Max Memory</Label>
              {editMaxRamValue === 0 && !editMaxRamSource && (
                <span className="text-xs text-muted-foreground">Unlimited</span>
              )}
            </div>
            <div className="flex gap-2">
              <Input
                id="edit-max-ram"
                type="number"
                min="0"
                value={editMaxRamValue}
                onChange={(e) => {
                  setEditMaxRamValue(Number(e.target.value));
                  setEditMaxRamSource(null);
                  setRamLimitDirty(true);
                }}
                placeholder="0 = Unlimited"
                className="flex-1"
              />
              <Select value={editMaxRamUnit} onValueChange={(v) => {
                setEditMaxRamUnit(v as LxdLimitUnit);
                setEditMaxRamSource(null);
                setRamLimitDirty(true);
              }}>
                <SelectTrigger className="w-[100px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="MiB">MiB</SelectItem>
                  <SelectItem value="GiB">GiB</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {editMaxRamSource && !ramLimitDirty && (
              <p className="text-xs text-muted-foreground">
                Current daemon value: {editMaxRamSource}. It remains unchanged until you edit this limit.
              </p>
            )}
          </div>

          {/* Max Storage */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="edit-max-storage">Max Storage</Label>
              {editMaxStorageValue === 0 && !editMaxStorageSource && (
                <span className="text-xs text-muted-foreground">Unlimited</span>
              )}
            </div>
            <div className="flex gap-2">
              <Input
                id="edit-max-storage"
                type="number"
                min="0"
                value={editMaxStorageValue}
                onChange={(e) => {
                  setEditMaxStorageValue(Number(e.target.value));
                  setEditMaxStorageSource(null);
                  setStorageLimitDirty(true);
                }}
                placeholder="0 = Unlimited"
                className="flex-1"
              />
              <Select value={editMaxStorageUnit} onValueChange={(v) => {
                setEditMaxStorageUnit(v as StorageLimitUnit);
                setEditMaxStorageSource(null);
                setStorageLimitDirty(true);
              }}>
                <SelectTrigger className="w-[100px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="MB">MB</SelectItem>
                  <SelectItem value="GB">GB</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {editMaxStorageSource && !storageLimitDirty && (
              <p className="text-xs text-muted-foreground">
                Current daemon value: {editMaxStorageSource}. It remains unchanged until you edit this limit.
              </p>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isSaving}
          >
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={isSaving}>
            {isSaving ? "Saving..." : "Save Changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
