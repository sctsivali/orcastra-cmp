"use client";

import { useState, useEffect } from "react";
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
import { Activity, Loader2, RotateCw, X, Clock } from "lucide-react";
import { OperationInfo, OperationListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";

export default function OperationsPage() {
  const [operations, setOperations] = useState<OperationInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  useEffect(() => {
    fetchOperations();
    // Auto-refresh every 5 seconds
    const interval = setInterval(fetchOperations, 5000);
    return () => clearInterval(interval);
  }, []);

  const fetchOperations = async () => {
    try {
      const response = await fetch(`${API_URL}/operations`);
      if (!response.ok) throw new Error("Failed to fetch operations");
      const data: OperationListResponse = await response.json();
      setOperations(data.operations);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to fetch operations",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Activity className="h-8 w-8" />
            Operations
          </h1>
          <p className="text-muted-foreground mt-2">
            Monitor LXD and MicroCloud operations and background tasks
          </p>
        </div>
        <Button onClick={fetchOperations}>
          <RotateCw className="h-4 w-4 mr-2" />
          Refresh
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
                {operations.map((operation) => (
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
