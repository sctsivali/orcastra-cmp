"use client";

import { Info } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { NodeStatus } from "@/types";

const EMPTY = "N/A";

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  );
}

/**
 * The System tab.
 *
 * note: this is the one panel that reads only the list row and never NodeResources. That is
 * the split axis for this directory, and it is what lets the modal leave the live per-cluster
 * poll switched off while an operator is reading kernel versions.
 */
export function NodeSystemPanel({ node }: { node: NodeStatus }) {
  const env = node.environment;
  const osName = String(env?.os_name || EMPTY);
  const osVersion = String(env?.os_version || "");

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Info className="h-5 w-5" />
          System Information
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Server Version" value={node.server_version || EMPTY} />
          <Field label="API Version" value={node.api_version || EMPTY} />
          <Field label="Authentication" value={node.auth || EMPTY} />
          <Field label="Server Name" value={String(env?.server_name || EMPTY)} />
          <Field label="OS" value={osVersion ? `${osName} ${osVersion}` : osName} />
          <Field label="Kernel" value={String(env?.kernel_version || EMPTY)} />
          {/* note: never through formatLabel. It title-cases raw enums and turns x86_64 into "X86 64". */}
          <Field label="Architecture" value={String(env?.kernel_architecture || EMPTY)} />
          <Field label="Clustered" value={env?.server_clustered ? "Yes" : "No"} />
          <Field label="Storage Backend" value={String(env?.storage || EMPTY)} />
          <Field label="Firewall" value={String(env?.firewall || EMPTY)} />
        </div>
      </CardContent>
    </Card>
  );
}
