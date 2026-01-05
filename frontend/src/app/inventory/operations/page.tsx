"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Cog, RotateCw, X, Clock } from "lucide-react";
import { useOperations } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

export default function OperationsPage() {
  const { data, isLoading, refetch, isRefetching } = useOperations();
  const operations = (data as any)?.operations || [];

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case "success":
      case "completed":
        return "default";
      case "running":
        return "secondary";
      case "pending":
        return "outline";
      default:
        return "destructive";
    }
  };

  if (isLoading) {
    return <LoadingLottiePage />;
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Cog className="h-8 w-8" />
            Operations
          </h1>
          <p className="text-muted-foreground mt-2">
            Monitor hypervisor operations and background tasks
          </p>
        </div>
        <Button onClick={() => refetch()} disabled={isRefetching} size="sm" className="gap-2">
          <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Active Operations ({operations.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {operations.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No active operations
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Operation ID</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Class</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Can Cancel</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {operations.map((operation: any) => (
                  <TableRow key={operation.id}>
                    <TableCell className="font-mono text-xs">
                      {operation.id.substring(0, 8)}...
                    </TableCell>
                    <TableCell>{operation.description || "N/A"}</TableCell>
                    <TableCell>
                      <Badge variant={getStatusColor(operation.status)}>
                        {operation.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{operation.class_name}</Badge>
                    </TableCell>
                    <TableCell>
                      {new Date(operation.created_at).toLocaleString()}
                    </TableCell>
                    <TableCell>
                      {operation.may_cancel ? (
                        <Badge variant="secondary">Yes</Badge>
                      ) : (
                        <Badge variant="outline">No</Badge>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
