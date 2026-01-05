"use client";

import { useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { AlertTriangle, RotateCw, X, Info } from "lucide-react";
import { useWarnings } from "@/hooks/use-lxd";
import { LoadingLottiePage } from "@/components/loading-lottie";

export default function WarningsPage() {
  const { data, isLoading, refetch, isRefetching } = useWarnings();
  const warnings = (data as any)?.warnings || [];
  
  // State for warning detail modal
  const [selectedWarning, setSelectedWarning] = useState<any>(null);
  const [detailModalOpen, setDetailModalOpen] = useState(false);

  const handleShowWarningDetail = (warning: any) => {
    setSelectedWarning(warning);
    setDetailModalOpen(true);
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

  if (isLoading) {
    return <LoadingLottiePage />;
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
            View hypervisor system warnings and alerts
          </p>
        </div>
        <Button onClick={() => refetch()} disabled={isRefetching} size="sm" className="gap-2">
          <RotateCw className={`h-4 w-4 ${isRefetching ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">Refresh</span>
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
                {warnings.map((warning: any) => (
                  <TableRow 
                    key={warning.uuid}
                    className="cursor-pointer hover:bg-accent/50 transition-colors"
                    onClick={() => handleShowWarningDetail(warning)}
                  >
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

      {/* Warning Detail Modal */}
      <Dialog open={detailModalOpen} onOpenChange={setDetailModalOpen}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5" />
              Warning Details
            </DialogTitle>
            <DialogDescription>
              Detailed information about the system warning
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-4">
            {selectedWarning && (
              <>
                {/* Warning Type & Severity */}
                <div className="flex items-center gap-3">
                  <div>
                    <span className="text-sm font-medium text-muted-foreground">Type:</span>
                    <Badge variant="outline" className="ml-2">{selectedWarning.type}</Badge>
                  </div>
                  <div>
                    <span className="text-sm font-medium text-muted-foreground">Severity:</span>
                    <Badge variant={getSeverityColor(selectedWarning.severity)} className="ml-2">
                      {selectedWarning.severity}
                    </Badge>
                  </div>
                  <div>
                    <span className="text-sm font-medium text-muted-foreground">Status:</span>
                    <Badge variant="secondary" className="ml-2">{selectedWarning.status}</Badge>
                  </div>
                </div>

                {/* Location & Project */}
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">Location:</span>
                    <p className="text-sm font-mono bg-secondary/50 p-2 rounded">
                      {selectedWarning.location || "N/A"}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">Project:</span>
                    <p className="text-sm font-mono bg-secondary/50 p-2 rounded">
                      {selectedWarning.project || "N/A"}
                    </p>
                  </div>
                </div>

                {/* Full Message */}
                <div className="space-y-2">
                  <span className="text-sm font-medium text-muted-foreground">Full Message:</span>
                  <div className="p-4 bg-secondary/50 rounded-md border border-border">
                    <p className="text-sm whitespace-pre-wrap break-words">
                      {selectedWarning.last_message || "No message available"}
                    </p>
                  </div>
                </div>

                {/* Occurrence Info */}
                <div className="grid grid-cols-3 gap-4 pt-4 border-t">
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">Count:</span>
                    <p className="text-lg font-semibold">{selectedWarning.count}</p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">First Seen:</span>
                    <p className="text-sm">
                      {selectedWarning.first_seen_at 
                        ? new Date(selectedWarning.first_seen_at).toLocaleString()
                        : "N/A"}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-sm font-medium text-muted-foreground">Last Seen:</span>
                    <p className="text-sm">
                      {selectedWarning.last_seen_at 
                        ? new Date(selectedWarning.last_seen_at).toLocaleString()
                        : "N/A"}
                    </p>
                  </div>
                </div>

                {/* UUID */}
                <div className="pt-4 border-t">
                  <span className="text-xs font-medium text-muted-foreground">UUID:</span>
                  <p className="text-xs font-mono bg-secondary/30 p-2 rounded mt-1">
                    {selectedWarning.uuid}
                  </p>
                </div>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
