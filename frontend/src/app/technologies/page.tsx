"use client";

import { useQuery } from "@tanstack/react-query";
import { ServerOff } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { HardwareSummary } from "@/components/technologies/hardware-summary";
import { NodeHardwareCard } from "@/components/technologies/node-hardware-card";
import { SoftwareInventory } from "@/components/technologies/software-inventory";
import { api } from "@/lib/api";
import type { NodeStatus } from "@/types/api";

// Shown when no cluster is connected yet, so the page never implies data exists when it does not.
function NoClusterState({ what }: { what: string }) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center justify-center text-center py-16 px-6">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted mb-4">
          <ServerOff className="h-6 w-6 text-muted-foreground" />
        </div>
        <h3 className="text-lg font-semibold mb-1">No cluster connected yet</h3>
        <p className="text-sm text-muted-foreground max-w-md">
          {what} details appear once a cluster is registered and connected. Register a cluster, then
          this page shows live data aggregated from its nodes.
        </p>
      </CardContent>
    </Card>
  );
}

export default function TechnologiesPage() {
  const { data, isLoading } = useQuery<{ nodes: NodeStatus[]; all_connected: boolean }>({
    queryKey: ["nodes"],
    queryFn: api.nodes.status,
  });

  const nodes = data?.nodes || [];
  const hasNodes = nodes.length > 0;

  if (isLoading) {
    return (
      <div className="container py-8">
        <LoadingLottiePage />
      </div>
    );
  }

  return (
    <div className="container py-8 space-y-8">
      <div>
        <h1 className="text-4xl font-bold mb-2">Technologies</h1>
        <p className="text-muted-foreground text-lg">
          The hardware and software powering this Orcastra platform, read live from the cluster
        </p>
      </div>

      <Tabs defaultValue="hardware" className="w-full">
        <TabsList className="grid w-full max-w-md grid-cols-2 h-auto">
          <TabsTrigger value="hardware" className="py-2.5">Hardware</TabsTrigger>
          <TabsTrigger value="software" className="py-2.5">Software</TabsTrigger>
        </TabsList>

        <TabsContent value="hardware" className="space-y-6 mt-6">
          {!hasNodes ? (
            <NoClusterState what="Hardware" />
          ) : (
            <>
              <HardwareSummary nodes={nodes} />
              <div className="space-y-3">
                <h2 className="text-lg font-semibold">Node Detail</h2>
                {nodes.map((node) => (
                  <NodeHardwareCard key={node.name} node={node} />
                ))}
              </div>
            </>
          )}
        </TabsContent>

        <TabsContent value="software" className="space-y-6 mt-6">
          {!hasNodes ? <NoClusterState what="Software" /> : <SoftwareInventory nodes={nodes} />}
        </TabsContent>
      </Tabs>
    </div>
  );
}
