"use client";

import * as React from "react";
import { Info, Server } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import type { NodeStatus } from "@/types/api";
import { fleetDivergence } from "./software-fields";
import { SoftwareNodeCard } from "./software-node-card";

/**
 * The software inventory, one card per connected node.
 *
 * This replaces a single set of cards built from connectedNodes[0]. On a fleet of Ubuntu 24.04
 * on x86_64 and Ubuntu 22.04 on aarch64 that read as "the platform runs 24.04", which is the
 * same class of untrue statement the hardcoded values on this page used to make: one node
 * quietly speaking for all of them.
 */
export function SoftwareInventory({ nodes }: { nodes: NodeStatus[] }) {
  const connected = nodes.filter((node) => node.connected && node.environment);
  const unreachable = nodes.filter((node) => !node.connected);
  const divergence = fleetDivergence(connected);

  if (connected.length === 0) {
    return (
      <Card>
        <CardContent className="pt-6">
          <EmptyState
            icon={Server}
            title="No node reports a software inventory yet"
            description="Versions appear here once a node is connected and answering."
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <>
      <Card className="bg-muted/50">
        <CardContent className="p-6">
          <div className="flex items-start gap-3">
            <Info className="h-5 w-5 text-blue-500 mt-0.5" aria-hidden="true" />
            <div>
              <p className="text-sm font-medium mb-1">Live Production Software Inventory</p>
              <p className="text-sm text-muted-foreground">
                {connected.length === 1
                  ? "Reported by the one connected node. A missing version means the node does not report that component, not that it is absent."
                  : `Reported separately by each of the ${connected.length} connected nodes, because they do not have to agree. A missing version means the node does not report that component, not that it is absent.`}
              </p>
              {divergence.length > 0 ? (
                <p className="text-sm text-muted-foreground mt-1">
                  This fleet is running {divergence.join(", ")}.
                </p>
              ) : null}
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6 items-start">
        {connected.map((node) => (
          <SoftwareNodeCard key={node.name} node={node} />
        ))}
      </div>

      {unreachable.length > 0 ? (
        <Card>
          <CardContent className="pt-6 space-y-2">
            <p className="text-sm font-medium">
              {unreachable.length} registered {unreachable.length === 1 ? "node is" : "nodes are"}{" "}
              not answering
            </p>
            <ul className="space-y-1">
              {unreachable.map((node) => (
                <li
                  key={node.name}
                  className="flex flex-wrap items-baseline gap-x-2 text-sm text-muted-foreground"
                >
                  <span className="font-medium text-foreground">
                    {node.display_name || node.name}
                  </span>
                  <span className="break-words">{node.error || "no reason reported"}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
    </>
  );
}
