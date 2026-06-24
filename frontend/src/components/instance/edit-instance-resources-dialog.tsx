"use client";

/**
 * Edit Resources dialog - live edit of an instance's CPU and memory limits.
 * Mirrors the Edit Project dialog layout. Applies changes live where the platform
 * supports it; if a restart is required the dialog surfaces a banner with a
 * one-click restart instead of restarting automatically.
 */

import * as React from "react";
import { Cpu, MemoryStick, AlertTriangle, RotateCw } from "lucide-react";
import { Spinner } from "@/components/ui/spinner";

import { VMInfo } from "@/types";
import { useUpdateInstanceResources, useRestartVM, useServerResources } from "@/hooks/use-lxd";
import { toast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

type MemUnit = "GiB" | "MiB";

interface EditInstanceResourcesDialogProps {
  vm: VMInfo;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// LXD memory string -> a value + unit we can show in the form.
function parseMemory(raw?: string): { value: number; unit: MemUnit } {
  if (!raw) return { value: 0, unit: "GiB" };
  const m = raw.trim().match(/^(\d+(?:\.\d+)?)\s*(GiB|MiB|GB|MB|B)?$/i);
  if (!m) return { value: 0, unit: "GiB" };
  const value = parseFloat(m[1]);
  const unit = (m[2] || "").toUpperCase();
  if (unit === "MIB" || unit === "MB") return { value, unit: "MiB" };
  if (unit === "GIB" || unit === "GB") return { value, unit: "GiB" };
  // bare number = bytes: pick the friendlier unit
  const gib = value / 1024 ** 3;
  if (gib >= 1) return { value: Math.round(gib * 100) / 100, unit: "GiB" };
  return { value: Math.round((value / 1024 ** 2) * 100) / 100, unit: "MiB" };
}

function memToBytes(value: number, unit: MemUnit): number {
  return unit === "GiB" ? value * 1024 ** 3 : value * 1024 ** 2;
}

// limits.cpu is a plain count only when it is a single integer (not a pinned set).
function parseCpuCount(raw?: string): { cores: number; pinned: boolean } {
  if (!raw) return { cores: 0, pinned: false };
  if (/^\d+$/.test(raw.trim())) return { cores: parseInt(raw, 10), pinned: false };
  return { cores: 0, pinned: true };
}

export function EditInstanceResourcesDialog({
  vm,
  open,
  onOpenChange,
}: EditInstanceResourcesDialogProps) {
  const updateResources = useUpdateInstanceResources();
  const restartVM = useRestartVM();
  const { data: nodeResources } = useServerResources(open ? vm.host : "");

  const project = vm.project || "default";
  const initialCpu = React.useMemo(() => parseCpuCount(vm.config?.["limits.cpu"]), [vm]);
  const initialMem = React.useMemo(() => parseMemory(vm.config?.["limits.memory"]), [vm]);

  const [cpuCores, setCpuCores] = React.useState(initialCpu.cores);
  const [memValue, setMemValue] = React.useState(initialMem.value);
  const [memUnit, setMemUnit] = React.useState<MemUnit>(initialMem.unit);
  const [saving, setSaving] = React.useState(false);
  const [restartRequired, setRestartRequired] = React.useState(false);
  const [restarting, setRestarting] = React.useState(false);

  // Re-seed the form whenever the dialog (re)opens for an instance.
  React.useEffect(() => {
    if (open) {
      setCpuCores(initialCpu.cores);
      setMemValue(initialMem.value);
      setMemUnit(initialMem.unit);
      setRestartRequired(false);
    }
  }, [open, initialCpu, initialMem]);

  const cpuChanged = cpuCores !== initialCpu.cores;
  const memChanged = memValue !== initialMem.value || memUnit !== initialMem.unit;
  const hasChanges = cpuChanged || memChanged;

  // Best-effort capacity warning against the node's totals (non-blocking).
  // serverAPI.getResources returns the LXD resources shape (cpu.total, memory.total).
  const res = nodeResources as { cpu?: { total?: number }; memory?: { total?: number } } | undefined;
  const cpuTotal = res?.cpu?.total;
  const memTotal = res?.memory?.total;
  const cpuOver = cpuChanged && cpuCores > 0 && typeof cpuTotal === "number" && cpuCores > cpuTotal;
  const memOver =
    memChanged &&
    memValue > 0 &&
    typeof memTotal === "number" &&
    memToBytes(memValue, memUnit) > memTotal;
  const capacityWarning = cpuOver || memOver;

  const handleSave = async () => {
    if (!hasChanges) return;
    setSaving(true);
    try {
      const result = await updateResources.mutateAsync({
        name: vm.name,
        host: vm.host,
        project,
        cpu_limit: cpuChanged ? (cpuCores === 0 ? "0" : String(cpuCores)) : undefined,
        memory_limit: memChanged ? (memValue === 0 ? "0" : `${memValue}${memUnit}`) : undefined,
      });

      if (result.restart_required) {
        setRestartRequired(true);
        toast({
          title: "Resources saved",
          description: "A restart is required for the change to take effect.",
        });
      } else {
        toast({
          title: "Resources updated",
          description: result.message || `${vm.name} resources updated.`,
          variant: "success",
        });
        onOpenChange(false);
      }
    } catch (error) {
      toast({
        title: "Failed to update resources",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleRestart = async () => {
    setRestarting(true);
    try {
      await restartVM.mutateAsync({ name: vm.name, request: { host: vm.host, project } });
      toast({
        title: "Restarting",
        description: `${vm.name} is restarting to apply the new resources.`,
        variant: "success",
      });
      onOpenChange(false);
    } catch (error) {
      toast({
        title: "Failed to restart",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setRestarting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>Edit Resources</DialogTitle>
          <DialogDescription>
            Update CPU and memory limits for {vm.name}. Changes apply live on
            containers; virtual machines may need a restart.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {/* CPU Cores */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="edit-cpu" className="flex items-center gap-2">
                <Cpu className="h-4 w-4" /> CPU Cores
              </Label>
              {cpuCores === 0 && (
                <span className="text-xs text-muted-foreground">Inherited / unlimited</span>
              )}
            </div>
            <Input
              id="edit-cpu"
              type="number"
              min="0"
              value={cpuCores}
              onChange={(e) => setCpuCores(Number(e.target.value))}
              placeholder="0 = inherit from profile"
            />
            {initialCpu.pinned && (
              <p className="text-xs text-muted-foreground">
                Current limit is a pinned CPU set ({vm.config?.["limits.cpu"]}). Saving a number
                replaces it with a core count.
              </p>
            )}
          </div>

          {/* Memory */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="edit-mem" className="flex items-center gap-2">
                <MemoryStick className="h-4 w-4" /> Memory
              </Label>
              {memValue === 0 && (
                <span className="text-xs text-muted-foreground">Inherited / unlimited</span>
              )}
            </div>
            <div className="flex gap-2">
              <Input
                id="edit-mem"
                type="number"
                min="0"
                value={memValue}
                onChange={(e) => setMemValue(Number(e.target.value))}
                placeholder="0 = inherit from profile"
                className="flex-1"
              />
              <Select value={memUnit} onValueChange={(v) => setMemUnit(v as MemUnit)}>
                <SelectTrigger className="w-[100px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="MiB">MiB</SelectItem>
                  <SelectItem value="GiB">GiB</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          {capacityWarning && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-700 dark:text-amber-400">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
              <span>
                The requested {cpuOver && memOver ? "CPU and memory" : cpuOver ? "CPU" : "memory"}{" "}
                exceeds this node&apos;s capacity. You can still save (overcommit), but the instance
                may not get the full amount.
              </span>
            </div>
          )}

          {restartRequired && (
            <div className="flex items-start gap-2 rounded-md border border-blue-500/40 bg-blue-500/10 p-3 text-sm text-blue-700 dark:text-blue-400">
              <RotateCw className="h-4 w-4 mt-0.5 shrink-0" />
              <span>
                Saved, but a restart is required for the new resources to take effect.
              </span>
            </div>
          )}
        </div>

        <DialogFooter>
          {restartRequired ? (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={restarting}>
                Later
              </Button>
              <Button onClick={handleRestart} disabled={restarting}>
                {restarting ? (
                  <>
                    <Spinner className="h-4 w-4 mr-2" /> Restarting...
                  </>
                ) : (
                  "Restart now"
                )}
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
                Cancel
              </Button>
              <Button onClick={handleSave} disabled={saving || !hasChanges}>
                {saving ? (
                  <>
                    <Spinner className="h-4 w-4 mr-2" /> Saving...
                  </>
                ) : (
                  "Save Changes"
                )}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
