"use client";

import { useState, useEffect } from "react";
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
import { Server, Loader2, RotateCw, Cpu, HardDrive } from "lucide-react";
import { ServerInfo, ServerListResponse } from "@/types/api";
import { useToast } from "@/hooks/use-toast";
import { API_URL } from "@/lib/api";

function ConfigItemsBadge({ config }: { config: Record<string, any> }) {
  const [open, setOpen] = useState(false);
  const itemCount = Object.keys(config).length;

  if (itemCount === 0) {
    return <Badge variant="outline">0 items</Badge>;
  }

  return (
    <>
      <Badge
        variant="secondary"
        className="cursor-pointer hover:bg-secondary/80 transition-colors"
        onClick={() => setOpen(true)}
      >
        {itemCount} items
      </Badge>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Configuration Items</DialogTitle>
            <DialogDescription>
              {itemCount} configuration item{itemCount !== 1 ? 's' : ''}
            </DialogDescription>
          </DialogHeader>

          <div className="mt-4 space-y-2">
            {Object.entries(config).map(([key, value], index) => (
              <div key={index} className="flex items-start gap-3 p-3 rounded-lg border bg-card hover:bg-muted/50 transition-colors">
                <div className="flex items-center justify-center w-8 h-8 rounded-full bg-primary/10 text-primary font-semibold text-sm flex-shrink-0">
                  {index + 1}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-sm font-mono break-all">{key}</div>
                  <div className="text-xs text-muted-foreground mt-1 break-all">
                    {typeof value === 'object' ? (
                      <pre className="text-xs bg-muted p-2 rounded mt-1 overflow-x-auto">
                        {JSON.stringify(value, null, 2)}
                      </pre>
                    ) : (
                      <span className="font-mono">{String(value)}</span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

export default function ServerPage() {
  const [servers, setServers] = useState<ServerInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  useEffect(() => {
    fetchServers();
  }, []);

  const fetchServers = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/server`);
      if (!response.ok) throw new Error("Failed to fetch server info");
      const data: ServerListResponse = await response.json();
      setServers(data.servers);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to fetch server information",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
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
            <Server className="h-8 w-8" />
            Server Information
          </h1>
          <p className="text-muted-foreground mt-2">
            View LXD and MicroCloud server details and configuration
          </p>
        </div>
        <Button onClick={fetchServers}>
          <RotateCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {servers.map((server) => (
          <Card key={server.node}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Server className="h-5 w-5" />
                {server.node}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  API Information
                </div>
                <div className="space-y-2">
                  <div className="flex justify-between">
                    <span className="text-sm">API Version:</span>
                    <Badge variant="outline">{server.api_version}</Badge>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Authentication:</span>
                    <Badge variant="secondary">{server.auth}</Badge>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-sm">Public:</span>
                    <Badge variant={server.public ? "default" : "outline"}>
                      {server.public ? "Yes" : "No"}
                    </Badge>
                  </div>
                </div>
              </div>

              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Environment
                </div>
                <div className="space-y-1 max-h-32 overflow-y-auto">
                  {Object.entries(server.environment)
                    .filter(([key]) => !key.includes('certificate'))
                    .slice(0, 10)
                    .map(([key, value]) => (
                      <div key={key} className="text-xs break-all">
                        <span className="font-mono text-muted-foreground">{key}:</span>{" "}
                        <span className="font-mono break-words">
                          {typeof value === 'object' ? JSON.stringify(value).slice(0, 50) + '...' : String(value).slice(0, 80)}
                        </span>
                      </div>
                    ))}
                </div>
              </div>

              <div>
                <div className="text-sm font-medium text-muted-foreground mb-2">
                  Configuration
                </div>
                <ConfigItemsBadge config={server.config} />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
