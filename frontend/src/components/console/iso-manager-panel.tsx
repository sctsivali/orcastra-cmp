/**
 * ISO Manager Panel - Manage ISO/image attachments for VMs.
 * Allows attaching, detaching, and browsing available ISO files.
 */

"use client";

import * as React from "react";
import { useState, useEffect, useCallback } from "react";
import {
  Disc,
  Loader2,
  CircleSlash,
  Download,
  RefreshCw,
  AlertCircle,
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
import { API_URL, getAccessToken } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useConsoleStore, AttachedISO } from "@/lib/console-store";

interface ISOInfo {
  name: string;
  pool: string;
  volume: string;
  path: string;
  size: number;
  createdAt?: string;
}

interface ISOManagerPanelProps {
  consoleId: string;
  instanceName: string;
  host: string;
  project: string;
  className?: string;
}

function formatSize(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
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
  const [bootPriority, setBootPriority] = useState(0);
  const [loading, setLoading] = useState(true);
  const [attaching, setAttaching] = useState(false);
  const [ejecting, setEjecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmEject, setConfirmEject] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [refreshingISOs, setRefreshingISOs] = useState(false);

  const { setAttachedISO } = useConsoleStore();

  // Fetch attached ISO
  const fetchAttachedISO = useCallback(async () => {
    try {
      const response = await fetch(
        `${API_URL}/console/${encodeURIComponent(instanceName)}/iso?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
        { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
      );

      if (!response.ok) throw new Error('Failed to fetch attached ISO');

      const data = await response.json();
      
      if (data.success && data.attached) {
        const iso: AttachedISO = {
          deviceName: data.device_name,
          source: data.source,
          pool: data.pool,
          bootPriority: data.boot_priority,
        };
        setAttachedISOState(iso);
        setAttachedISO(consoleId, iso);
      } else {
        setAttachedISOState(null);
        setAttachedISO(consoleId, undefined);
      }
    } catch (err) {
      console.error('Error fetching attached ISO:', err);
    }
  }, [consoleId, instanceName, host, project, setAttachedISO]);

  // Fetch available ISOs
  const fetchAvailableISOs = useCallback(async () => {
    try {
      const response = await fetch(
        `${API_URL}/console/isos?host=${encodeURIComponent(host)}`,
        { headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
      );

      if (!response.ok) throw new Error('Failed to fetch available ISOs');

      const data = await response.json();
      
      if (data.success) {
        setAvailableISOs(data.isos || []);
      }
    } catch (err) {
      console.error('Error fetching ISOs:', err);
    }
  }, [host]);

  // Initial load
  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        await Promise.all([fetchAttachedISO(), fetchAvailableISOs()]);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load');
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [fetchAttachedISO, fetchAvailableISOs]);

  // Attach ISO
  const handleAttach = async () => {
    if (!selectedISO) return;

    setAttaching(true);
    try {
      const response = await fetch(
        `${API_URL}/console/${encodeURIComponent(instanceName)}/iso/attach?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) },
          body: JSON.stringify({
            pool: selectedISO.pool,
            volume: selectedISO.volume,
            device_name: 'cdrom',
            boot_priority: bootPriority,
          }),
        }
      );

      if (!response.ok) throw new Error('Failed to attach ISO');

      const data = await response.json();
      
      if (data.success) {
        toast({
          title: "ISO Attached",
          description: data.message || `${selectedISO.name} attached successfully`,
        });
        await fetchAttachedISO();
        setSelectedISO(null);
      } else {
        throw new Error(data.error || 'Failed to attach');
      }
    } catch (err) {
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
      const response = await fetch(
        `${API_URL}/console/${encodeURIComponent(instanceName)}/iso/eject?host=${encodeURIComponent(host)}&project=${encodeURIComponent(project)}&device_name=${encodeURIComponent(attachedISO.deviceName)}`,
        { method: 'POST', headers: { ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}) } }
      );

      if (!response.ok) throw new Error('Failed to eject ISO');

      const data = await response.json();
      
      if (data.success) {
        toast({
          title: "ISO Ejected",
          description: data.message || "ISO ejected successfully",
        });
        setAttachedISOState(null);
        setAttachedISO(consoleId, undefined);
      } else {
        throw new Error(data.error || 'Failed to eject');
      }
    } catch (err) {
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

  const handleRefreshISOs = useCallback(async () => {
    setRefreshingISOs(true);
    try {
      await fetchAvailableISOs();
    } finally {
      setRefreshingISOs(false);
    }
  }, [fetchAvailableISOs]);

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
        <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
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
              <p className="font-medium truncate">{attachedISO.source}</p>
              <div className="flex items-center gap-2 mt-1">
                <Badge variant="secondary">Device: {attachedISO.deviceName}</Badge>
                <Badge variant="outline">Boot: {attachedISO.bootPriority}</Badge>
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
              disabled={refreshingISOs}
              isRefreshing={refreshingISOs}
              label="Refresh ISOs"
              onClick={handleRefreshISOs}
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
                    <p className="font-medium truncate text-sm">{iso.name}</p>
                    <div className="flex items-center gap-2 mt-1">
                      <Badge variant="outline" className="text-xs">
                        {iso.pool}
                      </Badge>
                      {iso.size > 0 && (
                        <span className="text-xs text-muted-foreground">
                          {formatSize(iso.size)}
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
                  <SelectItem value="0">0 - Highest (boot first)</SelectItem>
                  <SelectItem value="1">1</SelectItem>
                  <SelectItem value="2">2</SelectItem>
                  <SelectItem value="5">5</SelectItem>
                  <SelectItem value="10">10 - Lower</SelectItem>
                  <SelectItem value="99">99 - Lowest</SelectItem>
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
          
          <p className="text-xs text-muted-foreground mt-2 text-center">
            ⚠️ VM may need restart for changes to take effect
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
