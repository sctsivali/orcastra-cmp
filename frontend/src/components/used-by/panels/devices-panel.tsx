"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { DeviceIcon } from "@/components/detail/device-icon";
import { formatLabel } from "@/lib/utils";
import { Boxes } from "lucide-react";

export function DevicesPanel({ devices }: { devices?: Record<string, Record<string, string>> }) {
  const entries = React.useMemo(
    () => Object.entries(devices ?? {}).sort(([a], [b]) => a.localeCompare(b)),
    [devices],
  );

  if (entries.length === 0) {
    return <EmptyState icon={Boxes} title="No devices" description="Nothing is attached to this resource." />;
  }

  return (
    <div className="space-y-3">
      {entries.map(([name, config]) => {
        const type = String(config?.type ?? "");
        return (
          <div key={name} className="space-y-3 rounded-lg border p-4">
            <div className="flex items-center justify-between gap-3">
              <span className="inline-flex min-w-0 items-center gap-3">
                <DeviceIcon type={type} />
                <span className="min-w-0">
                  <span className="block truncate font-semibold">{name}</span>
                  <span className="block text-xs text-muted-foreground">Device</span>
                </span>
              </span>
              {type ? <Badge variant="outline">{formatLabel(type)}</Badge> : null}
            </div>
            {Object.keys(config ?? {}).length > 0 && (
              <dl className="grid grid-cols-1 gap-2 border-t pt-3 md:grid-cols-2">
                {Object.entries(config).map(([key, value]) => (
                  <div key={key} className="rounded-md bg-muted/50 p-2">
                    <dt className="font-mono text-xs text-muted-foreground break-all">{key}</dt>
                    <dd className="mt-1 break-all text-sm font-medium">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
        );
      })}
    </div>
  );
}
