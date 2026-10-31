"use client";

import * as React from "react";
import { AlertTriangle, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { LoadingLottiePageEnhanced } from "@/components/loading-lottie";
import { useNodesStatus } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { APIError } from "@/types/api";
import { HardwareAggregate, buildHardwareCategories } from "./components/hardware-aggregate";
import { SoftwareInventory } from "./components/software-inventory";
import { TenantInstanceTechnologies } from "./components/tenant-instance-technologies";
import { useInstanceSpecifications } from "./use-instance-specifications";

function PageHeader({ tenant = false }: { tenant?: boolean }) {
  return (
    <div>
      <h1 className="text-4xl font-bold mb-2">Technologies</h1>
      <p className="text-muted-foreground text-lg">
        {tenant
          ? "Review the resources assigned to the instances your account can access"
          : "Explore the hardware and software powering the Orcastra platform"}
      </p>
    </div>
  );
}

function PlatformTechnologies() {
  // important: through the owning hook, not a bare useQuery on the same key. This page
  // subscribed to ["nodes"] with no options at all, so whether its hardware totals ever
  // refreshed depended on whether some other component happened to be mounted in the same
  // tab session to carry the interval.
  const { data: nodesResponse, isLoading, isFetching, isError, refetch } = useNodesStatus();

  const nodes = nodesResponse?.nodes || [];
  const hardwareInfo = React.useMemo(() => buildHardwareCategories(nodes), [nodes]);
  const unreachable = nodes.filter((node) => !node.connected);
  const unreachableReasons = Object.fromEntries(
    unreachable.map((node) => [node.name, node.error ?? ""]),
  );

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
              title="Could not load cluster data"
              description="The dashboard did not get an answer for this page. This says nothing about whether your clusters are running."
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
          <UnreachableNodesBanner
            nodes={unreachable.map((node) => node.name)}
            reasons={unreachableReasons}
            noun="hardware readings"
          />
          {isError && nodesResponse ? (
            <PartialReadNotice>
              The latest refresh failed. The last successful hardware reading remains visible.
            </PartialReadNotice>
          ) : null}
          {nodes.length > 0 && hardwareInfo ? (
            <HardwareAggregate
              categories={hardwareInfo}
              nodes={nodes}
              isRefreshing={isFetching}
              refreshError={isError}
            />
          ) : (
            <Card>
              <CardContent className="pt-6">
                <EmptyState
                  icon={Server}
                  title="No cluster data yet"
                  description="Hardware figures appear here once a cluster is connected and reporting its resources."
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

function TenantTechnologies() {
  const { data, isLoading, isFetching, isError, error, refetch } = useInstanceSpecifications();

  if (isLoading) {
    return (
      <div className="container py-8">
        <LoadingLottiePageEnhanced context="instances" />
      </div>
    );
  }

  if (isError && !data) {
    const denied = error instanceof APIError && (error.status === 401 || error.status === 403);
    return (
      <div className="container py-8 space-y-8">
        <PageHeader tenant />
        <Card>
          <CardContent className="pt-6">
            <EmptyState
              icon={AlertTriangle}
              title={denied ? "Instance specifications are not available" : "Could not load instance specifications"}
              description={
                denied
                  ? "Your current session does not have access to this view. Sign in again or contact your organization administrator."
                  : "The dashboard did not get an answer. Your instances may still be running normally."
              }
              action={
                denied ? undefined : (
                  <Button variant="outline" size="sm" onClick={() => void refetch()}>
                    Retry
                  </Button>
                )
              }
            />
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="container py-8">
        <LoadingLottiePageEnhanced context="instances" />
      </div>
    );
  }

  return (
    <div className="container py-8 space-y-8">
      <PageHeader tenant />
      {isError ? (
        <PartialReadNotice>
          The latest refresh failed. The last successful instance specifications remain visible.
        </PartialReadNotice>
      ) : null}
      <TenantInstanceTechnologies
        response={data}
        isRefreshing={isFetching}
        refreshError={isError}
      />
    </div>
  );
}

export default function TechnologiesPage() {
  const { isLoading, isPartner } = usePermissions();

  if (isLoading) {
    return (
      <div className="container py-8">
        <LoadingLottiePageEnhanced context="data" />
      </div>
    );
  }

  // Separate mounted trees are a security boundary as well as a presentation choice. A Tenant
  // never mounts useNodesStatus, so the browser does not request platform inventory and briefly
  // hide it after the response arrives.
  return isPartner ? <PlatformTechnologies /> : <TenantTechnologies />;
}
