"use client";

import * as React from "react";
import { ChevronDown, Cpu, MemoryStick, Database, Monitor } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ServerHardware } from "@/components/server/server-hardware";
import { cn, formatBytes, formatHypervisorType } from "@/lib/utils";
import type { NodeStatus } from "@/types/api";

function QuickStat({ icon: Icon, value }: { icon: React.ElementType; value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <Icon className="h-3.5 w-3.5" />
      {value}
    </span>
  );
}

export function NodeHardwareCard({ node }: { node: NodeStatus }) {
  const [open, setOpen] = React.useState(false);
  const res = node.resources;
  const canExpand = node.connected && !!res;

  return (
    <Card>
      <Collapsible open={open} onOpenChange={canExpand ? setOpen : undefined}>
        <CollapsibleTrigger
          disabled={!canExpand}
          className={cn(
            "w-full text-left",
            canExpand ? "cursor-pointer" : "cursor-not-allowed opacity-90"
          )}
        >
          <CardContent className="p-4 flex items-center justify-between gap-4">
            <div className="min-w-0 space-y-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold truncate">{node.display_name || node.name}</span>
                {node.node_type && (
                  <Badge variant="outline" className="text-xs">
                    {formatHypervisorType(node.node_type)}
                  </Badge>
                )}
                <Badge variant={node.connected ? "success" : "secondary"} className="text-xs">
                  {node.connected ? "Connected" : "Offline"}
                </Badge>
              </div>
              {canExpand && res && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <QuickStat icon={Cpu} value={`${res.cpu_total} cores`} />
                  <QuickStat
                    icon={MemoryStick}
                    value={res.memory_total ? formatBytes(res.memory_total) : "—"}
                  />
                  <QuickStat
                    icon={Database}
                    value={res.storage_total ? formatBytes(res.storage_total) : "—"}
                  />
                  {res.gpu_total > 0 && (
                    <QuickStat icon={Monitor} value={`${res.gpu_total} GPU`} />
                  )}
                </div>
              )}
              {!canExpand && (
                <p className="text-xs text-muted-foreground">
                  {node.error ? node.error : "Hardware unavailable while the node is disconnected."}
                </p>
              )}
            </div>
            {canExpand && (
              <ChevronDown
                className={cn(
                  "h-5 w-5 text-muted-foreground shrink-0 transition-transform",
                  open && "rotate-180"
                )}
              />
            )}
          </CardContent>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="px-4 pb-4 pt-1 border-t">
            {/* Lazy: only fetch/render the heavy detail once the card is opened. */}
            {open && <ServerHardware node={node.name} hideSerials />}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}
