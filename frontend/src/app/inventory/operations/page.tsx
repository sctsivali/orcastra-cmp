"use client";

import { useState, useEffect, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { CopyableId } from "@/components/ui/copyable-id";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { X, Clock, Loader2, XCircle, Unplug } from "lucide-react";
import { useOperations } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { toast } from "@/hooks/use-toast";
import { API_URL, getAccessToken } from "@/lib/api";
import { usePermissions } from "@/hooks/use-permissions";
import { formatDateTime, formatLabel } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

export default function OperationsPage() {
  const router = useRouter();
  const { isPartner } = usePermissions();
  const { data, isLoading, refetch, isRefetching } = useOperations();
  const operations = useMemo(() => (data as any)?.operations || [], [data]);
  const nodes = (data as any)?.nodes || [];
  
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [killingId, setKillingId] = useState<string | null>(null);
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    operationId: string;
    host: string;
    description: string;
    isKillSession?: boolean;
    instanceName?: string;
  }>({ open: false, operationId: "", host: "", description: "" });

  // redirect if not Partner+
  useEffect(() => {
    if (!isPartner) {
      router.replace("/");
    }
  }, [isPartner, router]);

  // Sorting. Instance accessor mirrors the per-row derivation (instance_name, else
  // extract from a websocket console op's resources, else empty).
  const sortAccessors = useMemo(() => ({
    id: (o: any) => o.id,
    instance: (o: any) => {
      if (o.instance_name) return o.instance_name;
      if (o.class_name === "websocket" && o.description === "Executing command" && !o.metadata?.command) {
        const instances = o.metadata?.resources?.instances || [];
        if (instances.length > 0) {
          const match = String(instances[0]).match(/\/instances\/([^?]+)/);
          if (match) return match[1];
        }
      }
      return "";
    },
    description: (o: any) => o.description,
    node: (o: any) => o.node_name,
    project: (o: any) => o.project || "default",
    status: (o: any) => o.status,
    class: (o: any) => o.class_name,
    created: (o: any) => (o.created_at ? new Date(o.created_at).getTime() : null),
  }), []);
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "created",
    "desc",
  );
  const sortedOperations = useMemo(() => sortItems(operations), [operations, sortItems]);

  if (!isPartner) return null;

  // Find the host for an operation (check metadata, node_name, or iterate nodes)
  const findOperationHost = (operation: any): string => {
    // Check if operation has node_name from backend
    if (operation.node_name) {
      return operation.node_name;
    }
    // Check if operation has host in metadata
    if (operation.metadata?.host) {
      return operation.metadata.host;
    }
    // Default to first node if we can't determine
    return nodes[0] || "";
  };

  // Extract instance name from console operation description
  const extractInstanceName = (operation: any): string | null => {
    // Console operations have description like "Executing command" and metadata with fds
    if (operation.class_name === "websocket" && operation.description === "Executing command") {
      // Try to get from metadata
      if (operation.metadata?.command) {
        return null; // exec command, not console
      }
      // Check resources for instance name
      const resources = operation.metadata?.resources || {};
      const instances = resources.instances || [];
      if (instances.length > 0) {
        // Path format: /1.0/instances/name?project=xxx
        const path = instances[0];
        const match = path.match(/\/instances\/([^?]+)/);
        if (match) return match[1];
      }
    }
    return null;
  };

  const handleCancelOperation = async (operationId: string, host: string) => {
    setCancellingId(operationId);
    try {
      const response = await fetch(
        `${API_URL}/operations/${operationId}?host=${encodeURIComponent(host)}`,
        {
          method: "DELETE",
          headers: {
            ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
          },
        }
      );
      
      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: "Failed to cancel" }));
        throw new Error(error.detail || "Failed to cancel operation");
      }
      
      toast({
        title: "Operation cancelled",
        description: `Operation ${operationId.substring(0, 8)}... has been cancelled`,
      });
      
      // Refresh operations list
      refetch();
    } catch (error) {
      toast({
        title: "Failed to cancel operation",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setCancellingId(null);
      setConfirmDialog({ open: false, operationId: "", host: "", description: "" });
    }
  };

  const handleKillSession = async (operationId: string, instanceName: string, host: string) => {
    setKillingId(operationId);
    try {
      const response = await fetch(
        `${API_URL}/console/${instanceName}/active-sessions/${operationId}?host=${encodeURIComponent(host)}`,
        {
          method: "DELETE",
          headers: {
            ...(getAccessToken() ? { Authorization: `Bearer ${getAccessToken()}` } : {}),
          },
        }
      );
      
      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: "Failed to kill session" }));
        throw new Error(error.detail || "Failed to kill session");
      }
      
      toast({
        title: "Session killed",
        description: `Console session for ${instanceName} has been terminated`,
      });
      
      // Refresh operations list
      refetch();
    } catch (error) {
      toast({
        title: "Failed to kill session",
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setKillingId(null);
      setConfirmDialog({ open: false, operationId: "", host: "", description: "" });
    }
  };

  const openCancelConfirm = (operation: any, isKillSession = false, instanceName?: string) => {
    const host = findOperationHost(operation);
    setConfirmDialog({
      open: true,
      operationId: operation.id,
      host,
      description: operation.description || `Operation ${operation.id.substring(0, 8)}...`,
      isKillSession,
      instanceName,
    });
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">
            Operations
          </h1>
          <p className="text-muted-foreground mt-2">
            Monitor hypervisor operations and background tasks
          </p>
        </div>
        <RefreshButton
          disabled={isRefetching}
          isRefreshing={isRefetching}
          label="Refresh operations"
          onClick={() => refetch()}
        />
      </div>

      {/* Cancel/Kill Confirmation Dialog */}
      <AlertDialog open={confirmDialog.open} onOpenChange={(open) => setConfirmDialog((prev) => ({ ...prev, open }))}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmDialog.isKillSession ? "Kill Console Session?" : "Cancel Operation?"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmDialog.isKillSession ? (
                <>
                  Are you sure you want to kill this console session?
                  <br />
                  <span className="font-medium text-foreground">Instance: {confirmDialog.instanceName}</span>
                </>
              ) : (
                <>
                  Are you sure you want to cancel this operation?
                  <br />
                  <span className="font-medium text-foreground">{confirmDialog.description}</span>
                </>
              )}
              <br /><br />
              This action cannot be undone. The {confirmDialog.isKillSession ? "session" : "operation"} will be terminated immediately.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={cancellingId !== null && killingId !== null}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (confirmDialog.isKillSession && confirmDialog.instanceName) {
                  handleKillSession(confirmDialog.operationId, confirmDialog.instanceName, confirmDialog.host);
                } else {
                  handleCancelOperation(confirmDialog.operationId, confirmDialog.host);
                }
              }}
              disabled={cancellingId !== null || killingId !== null}
              className="bg-red-600 hover:bg-red-700"
            >
              {(cancellingId === confirmDialog.operationId || killingId === confirmDialog.operationId) ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {confirmDialog.isKillSession ? "Killing..." : "Cancelling..."}
                </>
              ) : (
                confirmDialog.isKillSession ? "Kill Session" : "Cancel Operation"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Card>
        <CardHeader>
          <CardTitle>Active Operations ({operations.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {operations.length === 0 ? (
            <EmptyState icon={Clock} title="No active operations" />
          ) : (
            <TooltipProvider>
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Operation ID" field="id" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Instance" field="instance" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Description" field="description" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Node" field="node" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Project" field="project" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Class" field="class" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Created" field="created" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sortedOperations.map((operation: any) => {
                    // Use instance_name from backend, or extract from metadata
                    const instanceName = operation.instance_name || extractInstanceName(operation) || "";
                    const isConsoleOp = operation.class_name === "websocket";
                    
                    return (
                    <TableRow key={operation.id}>
                      <TableCell>
                        <CopyableId value={operation.id} head={8} tail={6} label="Operation ID" />
                      </TableCell>
                      <TableCell>
                        {instanceName ? (
                          <span className="font-medium text-sm">{instanceName}</span>
                        ) : (
                          <span className="text-muted-foreground text-xs">-</span>
                        )}
                      </TableCell>
                      <TableCell>{operation.description || "N/A"}</TableCell>
                      <TableCell>
                        {operation.node_name || "N/A"}
                      </TableCell>
                      <TableCell>
                        <span className="text-sm">{operation.project || "default"}</span>
                      </TableCell>
                      <TableCell>
                        {operation.status}
                      </TableCell>
                      <TableCell>
                        {formatLabel(operation.class_name)}
                      </TableCell>
                      <TableCell className="text-sm">
                        {formatDateTime(operation.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        {operation.may_cancel ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8 text-red-500 hover:text-red-600 hover:bg-red-100"
                                onClick={() => openCancelConfirm(operation)}
                                disabled={cancellingId !== null || killingId !== null}
                              >
                                {cancellingId === operation.id ? (
                                  <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                  <XCircle className="h-4 w-4" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Cancel Operation</TooltipContent>
                          </Tooltip>
                        ) : isConsoleOp ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8 text-orange-500 hover:text-orange-600 hover:bg-orange-100"
                                onClick={() => openCancelConfirm(operation, true, instanceName || "N/A")}
                                disabled={cancellingId !== null || killingId !== null}
                              >
                                {killingId === operation.id ? (
                                  <Loader2 className="h-4 w-4 animate-spin" />
                                ) : (
                                  <Unplug className="h-4 w-4" />
                                )}
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Kill Console Session</TooltipContent>
                          </Tooltip>
                        ) : (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <span className="text-xs text-muted-foreground">
                                Cannot cancel
                              </span>
                            </TooltipTrigger>
                            <TooltipContent>This operation cannot be cancelled</TooltipContent>
                          </Tooltip>
                        )}
                      </TableCell>
                    </TableRow>
                  )})}
                </TableBody>
              </Table>
            </TooltipProvider>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
