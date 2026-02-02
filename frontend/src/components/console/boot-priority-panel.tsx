/**
 * Boot Priority Panel - Manage instance startup order.
 * Allows drag-and-drop reordering of instance boot sequence.
 */

"use client";

import * as React from "react";
import { useState, useEffect, useCallback } from "react";
import {
  GripVertical,
  Server,
  Box,
  Loader2,
  Save,
  RotateCcw,
  Clock,
  Power,
  PowerOff,
  AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { toast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";
import { cn } from "@/lib/utils";

interface StartupOrderEntry {
  instanceName: string;
  host: string;
  project: string;
  priority: number;
  delay: number;
  autostart: boolean;
  instanceType: string;
  status: string;
}

interface BootPriorityPanelProps {
  host: string;
  project: string;
  currentInstanceName?: string;
  className?: string;
}

export function BootPriorityPanel({
  host,
  project,
  currentInstanceName,
  className,
}: BootPriorityPanelProps) {
  const [entries, setEntries] = useState<StartupOrderEntry[]>([]);
  const [originalEntries, setOriginalEntries] = useState<StartupOrderEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);

  // Fetch startup order
  const fetchStartupOrder = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const response = await fetch(
        `${API_URL}/console/startup-order?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`
      );

      if (!response.ok) {
        throw new Error('Failed to fetch startup order');
      }

      const data = await response.json();
      
      if (data.success && data.entries) {
        // Convert snake_case to camelCase
        const converted = data.entries.map((e: any) => ({
          instanceName: e.instance_name,
          host: e.host,
          project: e.project,
          priority: e.priority,
          delay: e.delay,
          autostart: e.autostart,
          instanceType: e.instance_type,
          status: e.status,
        }));
        setEntries(converted);
        setOriginalEntries(JSON.parse(JSON.stringify(converted)));
      } else {
        throw new Error(data.error || 'Failed to fetch startup order');
      }
    } catch (err) {
      console.error('Error fetching startup order:', err);
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }, [host, project]);

  // Load on mount
  useEffect(() => {
    fetchStartupOrder();
  }, [fetchStartupOrder]);

  // Save changes
  const saveChanges = async () => {
    setSaving(true);

    try {
      // Build updates array
      const updates = entries.map((entry, index) => ({
        instance_name: entry.instanceName,
        priority: index, // Use index as priority
        delay: entry.delay,
        autostart: entry.autostart,
      }));

      const response = await fetch(
        `${API_URL}/console/startup-order?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ updates }),
        }
      );

      if (!response.ok) {
        throw new Error('Failed to save startup order');
      }

      const data = await response.json();

      if (data.success) {
        toast({
          title: "Saved",
          description: "Startup order updated successfully",
        });
        setOriginalEntries(JSON.parse(JSON.stringify(entries)));
      } else {
        throw new Error(data.error || 'Failed to save');
      }
    } catch (err) {
      console.error('Error saving startup order:', err);
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : 'Failed to save',
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  // Reset changes
  const resetChanges = () => {
    setEntries(JSON.parse(JSON.stringify(originalEntries)));
  };

  // Check if there are unsaved changes
  const hasChanges = JSON.stringify(entries) !== JSON.stringify(originalEntries);

  // Handle drag start
  const handleDragStart = (index: number) => {
    setDraggedIndex(index);
  };

  // Handle drag over
  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault();
    if (draggedIndex === null || draggedIndex === index) return;

    const newEntries = [...entries];
    const draggedEntry = newEntries[draggedIndex];
    newEntries.splice(draggedIndex, 1);
    newEntries.splice(index, 0, draggedEntry);
    setEntries(newEntries);
    setDraggedIndex(index);
  };

  // Handle drag end
  const handleDragEnd = () => {
    setDraggedIndex(null);
  };

  // Update entry
  const updateEntry = (index: number, field: keyof StartupOrderEntry, value: any) => {
    const newEntries = [...entries];
    newEntries[index] = { ...newEntries[index], [field]: value };
    setEntries(newEntries);
  };

  if (loading) {
    return (
      <div className={cn("flex items-center justify-center h-full", className)}>
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className={cn("flex flex-col items-center justify-center h-full gap-4", className)}>
        <AlertCircle className="h-8 w-8 text-destructive" />
        <p className="text-sm text-muted-foreground">{error}</p>
        <Button variant="outline" size="sm" onClick={fetchStartupOrder}>
          <RotateCcw className="h-4 w-4 mr-2" />
          Retry
        </Button>
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col h-full", className)}>
      {/* Header */}
      <div className="flex items-center justify-between p-4 border-b">
        <div>
          <h3 className="text-sm font-medium">Startup Order</h3>
          <p className="text-xs text-muted-foreground">
            Drag to reorder boot sequence
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={resetChanges}
            disabled={!hasChanges || saving}
          >
            <RotateCcw className="h-4 w-4 mr-1" />
            Reset
          </Button>
          <Button
            size="sm"
            onClick={saveChanges}
            disabled={!hasChanges || saving}
          >
            {saving ? (
              <Loader2 className="h-4 w-4 mr-1 animate-spin" />
            ) : (
              <Save className="h-4 w-4 mr-1" />
            )}
            Save
          </Button>
        </div>
      </div>

      {/* Entries list */}
      <ScrollArea className="flex-1">
        <div className="p-4 space-y-2">
          {entries.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              No instances found
            </p>
          ) : (
            entries.map((entry, index) => (
              <div
                key={entry.instanceName}
                draggable
                onDragStart={() => handleDragStart(index)}
                onDragOver={(e) => handleDragOver(e, index)}
                onDragEnd={handleDragEnd}
                className={cn(
                  "flex items-center gap-3 p-3 rounded-lg border bg-card transition-all",
                  "hover:border-primary/50 cursor-move",
                  draggedIndex === index && "opacity-50 border-primary",
                  entry.instanceName === currentInstanceName && "ring-2 ring-primary/30"
                )}
              >
                {/* Drag handle */}
                <GripVertical className="h-4 w-4 text-muted-foreground shrink-0" />

                {/* Priority badge */}
                <Badge variant="outline" className="shrink-0 w-8 justify-center">
                  {index + 1}
                </Badge>

                {/* Instance info */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    {entry.instanceType === 'virtual-machine' ? (
                      <Server className="h-4 w-4 text-blue-500 shrink-0" />
                    ) : (
                      <Box className="h-4 w-4 text-green-500 shrink-0" />
                    )}
                    <span className="font-medium truncate">{entry.instanceName}</span>
                    <Badge
                      variant={entry.status === 'Running' ? 'default' : 'secondary'}
                      className="shrink-0"
                    >
                      {entry.status}
                    </Badge>
                  </div>
                </div>

                {/* Delay input */}
                <div className="flex items-center gap-2 shrink-0">
                  <Clock className="h-4 w-4 text-muted-foreground" />
                  <Input
                    type="number"
                    min={0}
                    max={3600}
                    value={entry.delay}
                    onChange={(e) => updateEntry(index, 'delay', parseInt(e.target.value) || 0)}
                    className="w-16 h-8 text-xs"
                  />
                  <span className="text-xs text-muted-foreground">sec</span>
                </div>

                {/* Autostart toggle */}
                <div className="flex items-center gap-2 shrink-0">
                  <Switch
                    checked={entry.autostart}
                    onCheckedChange={(checked: boolean) => updateEntry(index, 'autostart', checked)}
                  />
                  {entry.autostart ? (
                    <Power className="h-4 w-4 text-green-500" />
                  ) : (
                    <PowerOff className="h-4 w-4 text-muted-foreground" />
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </ScrollArea>

      {/* Footer info */}
      <div className="p-3 border-t bg-muted/20">
        <p className="text-xs text-muted-foreground">
          💡 Instances with autostart enabled will start automatically when the hypervisor boots.
          Lower numbers start first. Delay adds wait time before starting.
        </p>
      </div>
    </div>
  );
}
