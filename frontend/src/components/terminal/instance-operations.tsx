/**
 * Instance Operations - Start/Stop/Restart buttons for terminal.
 * Controls the LXD instance lifecycle from within the terminal window.
 */

"use client";

import * as React from "react";
import { useState, useCallback } from "react";
import {
  Play,
  Square,
  RotateCcw,
  AlertTriangle,
} from "lucide-react";
import { Spinner } from "@/components/ui/spinner";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
import { cn } from "@/lib/utils";
import { LXDInstanceStatus, useTerminalStore } from "@/lib/terminal-store";
import { toast } from "@/hooks/use-toast";
import { API_URL, getAccessToken } from "@/lib/api";

interface InstanceOperationsProps {
  terminalId: string;
  instanceName: string;
  host: string;
  instanceStatus: LXDInstanceStatus;
}

type OperationType = 'start' | 'stop' | 'restart';

interface ConfirmDialogState {
  open: boolean;
  operation: OperationType | null;
  title: string;
  description: string;
}

export function InstanceOperations({
  terminalId,
  instanceName,
  host,
  instanceStatus,
}: InstanceOperationsProps) {
  const updateInstanceStatus = useTerminalStore(s => s.updateInstanceStatus);
  const updateTerminalStatus = useTerminalStore(s => s.updateTerminalStatus);
  
  const [loading, setLoading] = useState<OperationType | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>({
    open: false,
    operation: null,
    title: "",
    description: "",
  });

  // Execute instance operation
  const executeOperation = useCallback(async (operation: OperationType) => {
    setLoading(operation);
    
    // Update to transitional state
    const transitionStatus: Record<OperationType, LXDInstanceStatus> = {
      start: 'Starting',
      stop: 'Stopping',
      restart: 'Restarting',
    };
    updateInstanceStatus(terminalId, transitionStatus[operation]);

    try {
      const response = await fetch(`${API_URL}/instances/${encodeURIComponent(instanceName)}/${operation}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
        },
        body: JSON.stringify({
          host,
          force: false,
          timeout: 30,
        }),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(error.detail || `Failed to ${operation} instance`);
      }

      const result = await response.json();
      
      // Update instance status based on operation result
      // Backend returns new_status as string like "Running", "Stopped", etc.
      let newStatus: LXDInstanceStatus;
      if (result.new_status) {
        // Map backend status string to our LXDInstanceStatus type
        const statusMap: Record<string, LXDInstanceStatus> = {
          'running': 'Running',
          'stopped': 'Stopped',
          'frozen': 'Frozen',
          'error': 'Error',
          'Running': 'Running',
          'Stopped': 'Stopped',
          'Frozen': 'Frozen',
          'Error': 'Error',
        };
        newStatus = statusMap[result.new_status] || 'Unknown';
      } else {
        // Fallback based on operation
        newStatus = operation === 'start' ? 'Running' :
                   operation === 'stop' ? 'Stopped' :
                   'Running'; // restart ends in Running
      }
      
      updateInstanceStatus(terminalId, newStatus);
      
      // If stopped, update terminal connection status
      if (operation === 'stop') {
        updateTerminalStatus(terminalId, 'disconnected');
      }
      
      toast({
        title: `Instance ${operation}ed successfully`,
        description: `${instanceName} is now ${newStatus.toLowerCase()}`,
      });
      
    } catch (error) {
      console.error(`Failed to ${operation} instance:`, error);
      updateInstanceStatus(terminalId, 'Error');
      toast({
        title: `Failed to ${operation} instance`,
        description: error instanceof Error ? error.message : 'Unknown error occurred',
        variant: 'destructive',
      });
    } finally {
      setLoading(null);
    }
  }, [terminalId, instanceName, host, updateInstanceStatus, updateTerminalStatus]);

  // Show confirmation dialog for destructive operations
  const requestOperation = useCallback((operation: OperationType) => {
    if (operation === 'start') {
      // Start doesn't need confirmation
      executeOperation(operation);
      return;
    }

    const dialogConfig: Record<OperationType, { title: string; description: string }> = {
      start: { title: '', description: '' },
      stop: {
        title: `Stop ${instanceName}?`,
        description: `This will gracefully stop the instance. The terminal connection will be lost. You can start it again later.`,
      },
      restart: {
        title: `Restart ${instanceName}?`,
        description: `This will restart the instance. The terminal connection will be temporarily lost and will attempt to reconnect after restart.`,
      },
    };

    setConfirmDialog({
      open: true,
      operation,
      ...dialogConfig[operation],
    });
  }, [instanceName, executeOperation]);

  // Handle confirm dialog action
  const handleConfirm = useCallback(() => {
    if (confirmDialog.operation) {
      executeOperation(confirmDialog.operation);
    }
    setConfirmDialog(prev => ({ ...prev, open: false }));
  }, [confirmDialog.operation, executeOperation]);

  // Determine which buttons to show based on instance status
  const isRunning = instanceStatus === 'Running';
  const isStopped = instanceStatus === 'Stopped';
  const isTransitioning = ['Starting', 'Stopping', 'Restarting'].includes(instanceStatus);
  const isUnknownOrError = ['Unknown', 'Error'].includes(instanceStatus);
  
  // Show Start if stopped or unknown/error
  const showStart = isStopped || isUnknownOrError;
  // Show Restart/Stop if running or unknown/error
  const showRestartStop = isRunning || isUnknownOrError;
  
  // Disable all operations during transition
  const isDisabled = isTransitioning || loading !== null;

  return (
    <TooltipProvider delayDuration={100}>
      <div 
        className="flex items-center gap-0.5 mr-1 border-r border-gray-600 pr-1 touch-manipulation"
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
        onTouchStart={(e) => e.stopPropagation()}
        onTouchEnd={(e) => e.stopPropagation()}
        onPointerDown={(e) => e.stopPropagation()}
      >
        {/* Start button - show when stopped or unknown/error */}
        {showStart && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-green-400 hover:bg-green-400/10 touch-manipulation active:scale-95",
                  loading === 'start' && "text-green-400"
                )}
                onClick={() => requestOperation('start')}
                onTouchEnd={(e) => { e.preventDefault(); requestOperation('start'); }}
                disabled={isDisabled}
              >
                {loading === 'start' ? (
                  <Spinner className="h-4 w-4" />
                ) : (
                  <Play className="h-4 w-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>Start Instance</TooltipContent>
          </Tooltip>
        )}

        {/* Restart button - show when running or unknown/error */}
        {showRestartStop && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-blue-400 hover:bg-blue-400/10 touch-manipulation active:scale-95",
                  loading === 'restart' && "text-blue-400"
                )}
                onClick={() => requestOperation('restart')}
                onTouchEnd={(e) => { e.preventDefault(); requestOperation('restart'); }}
                disabled={isDisabled}
              >
                {loading === 'restart' ? (
                  <Spinner className="h-4 w-4" />
                ) : (
                  <RotateCcw className="h-4 w-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>Restart Instance</TooltipContent>
          </Tooltip>
        )}

        {/* Stop button - show when running or unknown/error */}
        {showRestartStop && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-red-400 hover:bg-red-400/10 touch-manipulation active:scale-95",
                  loading === 'stop' && "text-red-400"
                )}
                onClick={() => requestOperation('stop')}
                onTouchEnd={(e) => { e.preventDefault(); requestOperation('stop'); }}
                disabled={isDisabled}
              >
                {loading === 'stop' ? (
                  <Spinner className="h-4 w-4" />
                ) : (
                  <Square className="h-4 w-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>Stop Instance</TooltipContent>
          </Tooltip>
        )}

        {/* Transitioning indicator */}
        {isTransitioning && !loading && (
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="h-8 w-8 min-h-[32px] min-w-[32px] flex items-center justify-center">
                <Spinner className="h-4 w-4 text-yellow-400" />
              </div>
            </TooltipTrigger>
            <TooltipContent>{instanceStatus}...</TooltipContent>
          </Tooltip>
        )}
      </div>

      {/* Confirmation Dialog */}
      <AlertDialog open={confirmDialog.open} onOpenChange={(open) => setConfirmDialog(prev => ({ ...prev, open }))}>
        <AlertDialogContent className="bg-terminal border-terminal-border">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-gray-100">
              {confirmDialog.title}
            </AlertDialogTitle>
            <AlertDialogDescription className="text-gray-400">
              {confirmDialog.description}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="bg-gray-700 text-gray-200 hover:bg-gray-600 border-gray-600">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirm}
              className={cn(
                confirmDialog.operation === 'stop' && "bg-red-600 hover:bg-red-700",
                confirmDialog.operation === 'restart' && "bg-blue-600 hover:bg-blue-700"
              )}
            >
              {confirmDialog.operation === 'stop' ? 'Stop Instance' : 'Restart Instance'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </TooltipProvider>
  );
}
