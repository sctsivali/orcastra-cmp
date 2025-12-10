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
import { AlertTriangle, Loader2, RotateCw, X } from "lucide-react";
import { WarningInfo, WarningListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";

export default function WarningsPage() {
  const [warnings, setWarnings] = useState<WarningInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  useEffect(() => {
    fetchWarnings();
  }, []);

  const fetchWarnings = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/warnings`);
      if (!response.ok) throw new Error("Failed to fetch warnings");
      const data: WarningListResponse = await response.json();
      setWarnings(data.warnings);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to fetch warnings",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const getSeverityColor = (severity: string) => {
    switch (severity.toLowerCase()) {
      case "high":
      case "critical":
        return "destructive";
      case "medium":
      case "warning":
        return "default";
      case "low":
      case "info":
        return "secondary";
      default:
        return "outline";
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
            <AlertTriangle className="h-8 w-8" />
            Warnings
          </h1>
          <p className="text-muted-foreground mt-2">
            View LXD and MicroCloud system warnings and alerts
          </p>
        </div>
        <Button onClick={fetchWarnings}>
          <RotateCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>System Warnings ({warnings.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {warnings.length === 0 ? (
            <div className="text-center py-8 text-muted-foreground">
              No warnings found
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Type</TableHead>
                  <TableHead>Message</TableHead>
                  <TableHead>Severity</TableHead>
                  <TableHead>Location</TableHead>
                  <TableHead>Count</TableHead>
                  <TableHead>Last Seen</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {warnings.map((warning) => (
                  <TableRow key={warning.uuid}>
                    <TableCell>
                      <Badge variant="outline">{warning.type}</Badge>
                    </TableCell>
                    <TableCell className="max-w-md truncate">
                      {warning.last_message}
                    </TableCell>
                    <TableCell>
                      <Badge variant={getSeverityColor(warning.severity)}>
                        {warning.severity}
                      </Badge>
                    </TableCell>
                    <TableCell>{warning.location || "N/A"}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{warning.count}</Badge>
                    </TableCell>
                    <TableCell>
                      {new Date(warning.last_seen_at).toLocaleString()}
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
