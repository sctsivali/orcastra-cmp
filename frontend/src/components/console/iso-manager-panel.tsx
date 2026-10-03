/**
 * ISO Manager Panel - Manage ISO/image attachments for VMs.
 * Allows attaching, detaching, and browsing available ISO files.
 */

"use client";

import * as React from "react";
import { useState, useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Disc,
  Loader2,
  CircleSlash,
  Download,
  RefreshCw,
  AlertCircle,
  AlertTriangle,
  HardDrive,
  Check,
  ChevronDown,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { toast } from "@/hooks/use-toast";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { isAuthActorSynchronized } from "@/lib/api";
import { consoleAPI } from "@/lib/console-api";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { cn, formatBytes } from "@/lib/utils";
import { useConsoleStore, AttachedISO } from "@/lib/console-store";
import { OverflowText } from "@/components/ui/overflow-text";
import { BOOT_PRIORITY_OPTIONS, DEFAULT_BOOT_PRIORITY, type ISOInfo } from "./iso-panel-options";

interface ISOManagerPanelProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  className?: string;
}

export function ISOManagerPanel({
  consoleId,
  instanceName,
  host,
  project,
  className,
}: ISOManagerPanelProps) {
  const [attachedISO, setAttachedISOState] = useState<AttachedISO | null>(null);
  const [availableISOs, setAvailableISOs] = useState<ISOInfo[]>([]);
  const [selectedISO, setSelectedISO] = useState<ISOInfo | null>(null);
  const [bootPriority, setBootPriority] = useState(DEFAULT_BOOT_PRIORITY);
  const [savingPriority, setSavingPriority] = useState(false);
  const [loading, setLoading] = useState(true);
  const [attaching, setAttaching] = useState(false);
  const [ejecting, setEjecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmEject, setConfirmEject] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const { setAttachedISO } = useConsoleStore();
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const target = JSON.stringify([consoleId, host, project, instanceName, scopeKey]);
  const targetRef = React.useRef(target);
  targetRef.current = target;
  const isCurrent = useCallback(
    () => targetRef.current === target && isAuthActorSynchronized(scopeKey),
    [scopeKey, target],
  );

  // Fetch attached ISO
  const fetchAttachedISO = useCallback(async () => {
    try {
      const data = await consoleAPI.getAttachedISO(instanceName, host, project);
      if (!isCurrent()) return;
      
      if (data.success && data.attached) {
        const iso: AttachedISO = {
          deviceName: data.device_name || "cdrom",
          source: data.source || "",
          pool: data.pool || "",
          bootPriority: data.boot_priority,
        };
        setAttachedISOState(iso);
        setAttachedISO(consoleId, iso);
      } else {
        setAttachedISOState(null);
        setAttachedISO(consoleId, undefined);
      }
    } catch (err) {
      if (!isCurrent()) return;
      console.error('Error fetching attached ISO:', err);
    }
  }, [consoleId, host, instanceName, project, isCurrent, setAttachedISO]);

  // Fetch available ISOs
  const fetchAvailableISOs = useCallback(async () => {
    try {
      const data = await consoleAPI.listISOs(host, project);
      if (!isCurrent()) return;
      
      if (data.success) {
        setAvailableISOs((data.isos || []).map((iso) => ({
          ...iso,
          createdAt: iso.created_at ?? undefined,
        })));
      }
    } catch (err) {
      if (!isCurrent()) return;
      // important: rethrow. Swallowing this made a failed fetch render as an
      // empty ISO library, which reads as "this pool has no ISOs".
      console.error('Error fetching ISOs:', err);
      throw err;
    }
  }, [host, project, isCurrent]);

  const loadPanel = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([fetchAttachedISO(), fetchAvailableISOs()]);
    } catch (err) {
      if (!isCurrent()) return;
      setError(err instanceof Error ? err.message : 'Failed to load');
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [fetchAttachedISO, fetchAvailableISOs, isCurrent]);

  // Initial load
  useEffect(() => {
    targetRef.current = target;
    setAvailableISOs([]);
    setSelectedISO(null);
    setAttachedISOState(null);
    void loadPanel();
    return () => { if (targetRef.current === target) targetRef.current = ""; };
  }, [loadPanel, target]);

  // Change the boot priority of the already-attached ISO. Re-attaching the same
  // volume patches the existing device in place (attach_iso does an add/update),
  // which is how boot.priority is changed. There is no separate device-update
  // endpoint.
  const handleChangeBootPriority = async (priority: number) => {
    if (!attachedISO || !attachedISO.pool) return;

    setSavingPriority(true);
    try {
      const actorScope = scopeKey;
      await consoleAPI.attachISO(instanceName, host, project, {
        pool: attachedISO.pool,
        volume: attachedISO.source,
        device_name: attachedISO.deviceName,
        boot_priority: priority,
      });
      if (!isAuthActorSynchronized(actorScope)) return;
      await reconcileMutationImpact(queryClient, "instance.media");
      if (!isAuthActorSynchronized(actorScope)) return;

      toast({
        title: "Boot priority updated",
        description:
          priority > 0
            ? `"${attachedISO.source}" is set to boot priority ${priority}. Restart the VM to boot from it.`
            : `"${attachedISO.source}" is set to ${priority}. The VM will boot from its disk first.`,
      });
      await fetchAttachedISO();
    } catch (err) {
      if (!isAuthActorSynchronized(scopeKey)) return;
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : 'Failed to change boot priority',
        variant: "destructive",
      });
    } finally {
      setSavingPriority(false);
    }
  };

  // Attach ISO
  const handleAttach = async () => {
    if (!selectedISO) return;

    setAttaching(true);
    try {
      const actorScope = scopeKey;
      const data = await consoleAPI.attachISO(instanceName, host, project, {
        pool: selectedISO.pool,
        volume: selectedISO.volume,
        device_name: 'cdrom',
        boot_priority: bootPriority,
      });
      if (!isAuthActorSynchronized(actorScope)) return;
      await reconcileMutationImpact(queryClient, "instance.media");
      if (!isAuthActorSynchronized(actorScope)) return;
      toast({
        title: "ISO Attached",
        description: data.message || `${selectedISO.name} attached successfully`,
      });
      await fetchAttachedISO();
      if (!isAuthActorSynchronized(actorScope)) return;
      setSelectedISO(null);
    } catch (err) {
      if (!isAuthActorSynchronized(scopeKey)) return;
      console.error('Error attaching ISO:', err);
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : 'Failed to attach ISO',
        variant: "destructive",
      });
    } finally {
      setAttaching(false);
    }
  };

  // Eject ISO
  const handleEject = async () => {
    if (!attachedISO) return;

    setEjecting(true);
    setConfirmEject(false);

    try {
      const actorScope = scopeKey;
      const data = await consoleAPI.ejectISO(
        instanceName,
        host,
        project,
        attachedISO.deviceName,
      );
      if (!isAuthActorSynchronized(actorScope)) return;
      await reconcileMutationImpact(queryClient, "instance.media");
      if (!isAuthActorSynchronized(actorScope)) return;
      toast({
        title: "ISO Ejected",
        description: data.message || "ISO ejected successfully",
      });
      setAttachedISOState(null);
      setAttachedISO(consoleId, undefined);
    } catch (err) {
      if (!isAuthActorSynchronized(scopeKey)) return;
      console.error('Error ejecting ISO:', err);
      toast({
        title: "Error",
        description: err instanceof Error ? err.message : 'Failed to eject ISO',
        variant: "destructive",
      });
    } finally {
      setEjecting(false);
    }
  };

  // Filter ISOs by search
  const filteredISOs = availableISOs.filter(iso =>
    iso.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    iso.pool.toLowerCase().includes(searchQuery.toLowerCase())
  );

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
        <Button variant="outline" size="sm" onClick={() => void loadPanel()}>
          <RefreshCw className="h-4 w-4 mr-2" />
          Retry
        </Button>
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col h-full", className)}>
      {/* Currently attached */}
      <div className="p-4 border-b">
        <h3 className="text-sm font-medium mb-3">Current CD-ROM</h3>
        
        {attachedISO ? (
          <div className="flex items-center gap-3 p-3 rounded-lg border bg-card">
            <Disc className="h-8 w-8 text-blue-500 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="font-medium">
                <OverflowText value={attachedISO.source} label="source path for the attached ISO" mono />
              </p>
              <div className="flex items-center gap-2 mt-1">
                <Badge variant="secondary">Device: {attachedISO.deviceName}</Badge>
              </div>
              {/* Boot priority is editable in place: higher boots first, so this
                  is what decides whether the VM boots the ISO or its disk. */}
              <div className="flex items-center gap-2 mt-2">
                <Label className="text-xs shrink-0">Boot priority</Label>
                <Select
                  value={String(attachedISO.bootPriority)}
                  onValueChange={(v) => handleChangeBootPriority(parseInt(v))}
                  disabled={savingPriority}
                >
                  <SelectTrigger className="h-7 text-xs w-[15rem]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {BOOT_PRIORITY_OPTIONS.map((o) => (
                      <SelectItem key={o.value} value={String(o.value)}>
                        {o.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {savingPriority && <Loader2 className="h-3 w-3 animate-spin" />}
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setConfirmEject(true)}
              disabled={ejecting}
            >
              {ejecting ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <>
                  <CircleSlash className="h-4 w-4 mr-1" />
                  Eject
                </>
              )}
            </Button>
          </div>
        ) : (
          <div className="flex items-center gap-3 p-3 rounded-lg border bg-muted/30 text-muted-foreground">
            <Disc className="h-8 w-8 shrink-0" />
            <p className="text-sm">No ISO attached</p>
          </div>
        )}
      </div>

      {/* Available ISOs */}
      <div className="flex-1 flex flex-col min-h-0">
        <div className="p-4 pb-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium">Available ISOs</h3>
            <RefreshButton
              className="h-8 w-8"
              label="Refresh ISOs"
              onRefresh={fetchAvailableISOs}
              variant="ghost"
            />
          </div>
          
          <Input
            placeholder="Search ISOs..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="h-8"
          />
        </div>

        <ScrollArea className="flex-1 px-4">
          <div className="space-y-2 pb-4">
            {filteredISOs.length === 0 ? (
              <p className="text-sm text-muted-foreground text-center py-8">
                {searchQuery ? 'No ISOs match your search' : 'No ISOs available'}
              </p>
            ) : (
              filteredISOs.map((iso) => (
                <div
                  key={iso.path}
                  onClick={() => setSelectedISO(selectedISO?.path === iso.path ? null : iso)}
                  className={cn(
                    "flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-all",
                    "hover:border-primary/50",
                    selectedISO?.path === iso.path && "border-primary bg-primary/5"
                  )}
                >
                  <HardDrive className="h-5 w-5 text-muted-foreground shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="break-all text-sm font-medium">{iso.name}</p>
                    <div className="flex items-center gap-2 mt-1">
                      <Badge variant="outline" className="text-xs">
                        {iso.pool}
                      </Badge>
                      {iso.size > 0 && (
                        <span className="text-xs text-muted-foreground">
                          {formatBytes(iso.size, 1)}
                        </span>
                      )}
                    </div>
                  </div>
                  {selectedISO?.path === iso.path && (
                    <Check className="h-4 w-4 text-primary shrink-0" />
                  )}
                </div>
              ))
            )}
          </div>
        </ScrollArea>
      </div>

      {/* Attach controls */}
      {selectedISO && (
        <div className="p-4 border-t bg-muted/20">
          <div className="flex items-center gap-4 mb-3">
            <div className="flex-1">
              <Label className="text-xs">Boot Priority</Label>
              <Select
                value={String(bootPriority)}
                onValueChange={(v) => setBootPriority(parseInt(v))}
              >
                <SelectTrigger className="h-8 mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {BOOT_PRIORITY_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={String(o.value)}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          
          <Button
            className="w-full"
            onClick={handleAttach}
            disabled={attaching}
          >
            {attaching ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Download className="h-4 w-4 mr-2" />
            )}
            Attach {selectedISO.name}
          </Button>
          
          <p className="mt-2 flex items-center justify-center gap-2 text-xs text-muted-foreground">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>VM may need restart for changes to take effect</span>
          </p>
        </div>
      )}

      {/* Eject confirmation */}
      <AlertDialog open={confirmEject} onOpenChange={setConfirmEject}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Eject ISO?</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to eject the current ISO from {instanceName}?
              This may affect running processes if the ISO is in use.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleEject}>Eject</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
