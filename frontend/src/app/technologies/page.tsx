"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingLottiePageEnhanced } from "@/components/loading-lottie";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import type { NodeStatus } from "@/types/api";
import { HardwareAggregate, buildHardwareCategories } from "./components/hardware-aggregate";
import { NodeHardwareList } from "./components/node-hardware-list";
import { SoftwareInventory } from "./components/software-inventory";

function PageHeader() {
  return (
    <div>
      <h1 className="text-4xl font-bold mb-2">Technologies</h1>
      <p className="text-muted-foreground text-lg">
        Explore the hardware and software powering the Orcastra platform
      </p>
    </div>
  );
}

export default function TechnologiesPage() {
  const { data: nodesResponse, isLoading, isError, refetch } = useQuery<{ nodes: NodeStatus[]; all_connected: boolean }>({
    queryKey: ["nodes"],
    queryFn: () => api.nodes.status(),
  });

  const nodes = nodesResponse?.nodes || [];
  const hardwareInfo = React.useMemo(() => buildHardwareCategories(nodes), [nodes]);

  if (isLoading) {
    return (
      <div className="container py-8">
        <LoadingLottiePageEnhanced context="hardware" />
      </div>
    );
  }

  // important: a read that failed is not a fleet that is empty. `data` is undefined either
  // way, so without this branch the page falls through to the no-node empty state and tells
  // the operator their nodes are gone when the dashboard simply got no answer.
  //
  // note: only when there is nothing to show. A refetch can fail while the previous response
  // is still held (refetchOnReconnect is on), and replacing readable data with an error is
  // worse than showing it. Same rule as the sessions list.
  if (isError && !nodesResponse) {
    return (
      <div className="container py-8 space-y-8">
        <PageHeader />
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              icon={AlertTriangle}
              title="Could not load node data"
              description="The dashboard did not get an answer for this page. This says nothing about whether your nodes are running."
              action={
                <Button variant="outline" size="sm" onClick={() => void refetch()}>
                  Retry
                </Button>
              }
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container py-8 space-y-8">
      <PageHeader />

      <Tabs defaultValue="hardware" className="w-full">
        <TabsList className="grid w-full max-w-md grid-cols-2 h-auto">
          <TabsTrigger value="hardware" className="py-2.5">Hardware</TabsTrigger>
          <TabsTrigger value="software" className="py-2.5">Software</TabsTrigger>
        </TabsList>

        {/* Hardware Tab */}
        <TabsContent value="hardware" className="space-y-6 mt-6">
          {hardwareInfo ? (
            <>
              <HardwareAggregate categories={hardwareInfo} />
              <NodeHardwareList nodes={nodes} />
            </>
          ) : (
            <Card>
              <CardContent className="pt-6">
                <EmptyState
                  icon={Server}
                  title="No node data yet"
                  description="Hardware figures appear here once a node is connected and reporting its resources."
                />
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {/* Software Tab */}
        <TabsContent value="software" className="space-y-6 mt-6">
          <SoftwareInventory nodes={nodes} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
