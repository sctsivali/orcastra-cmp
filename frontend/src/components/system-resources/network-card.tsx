"use client";

import { ArrowDownToLine, ArrowUpFromLine, Wifi, WifiOff } from "lucide-react";
import type { NetworkInterfaceInfo } from "@/types";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useByteFormat } from "@/hooks/use-byte-format";
import { ratePerSecond } from "@/components/system-resources/rates";

const LOOPBACK_ADDRESSES = new Set(["127.0.0.1", "::1"]);

// Loopback traffic never leaves the container (health checks, local calls), so it says nothing
// about the network an operator is checking.
function isLoopback(iface: NetworkInterfaceInfo): boolean {
  return iface.name === "lo" || (iface.addresses.length > 0 && iface.addresses.every((a) => LOOPBACK_ADDRESSES.has(a)));
}

export function NetworkCard({
  network,
  scope,
  previous,
  timestamp,
}: {
  network: NetworkInterfaceInfo[];
  scope: "container" | "host";
  previous: { network: NetworkInterfaceInfo[]; timestamp: string } | null;
  timestamp: string;
}) {
  const shown = network.filter((iface) => !isLoopback(iface) && (iface.bytes_sent > 0 || iface.bytes_recv > 0 || iface.is_up));
  const up = shown.filter((iface) => iface.is_up).length;
  const since = scope === "container" ? "since container start" : "since boot";
  return (
    <Card>
      <CardHeader>
        <CardTitle>{scope === "container" ? "Container Network" : "Host Network"}</CardTitle>
        <CardDescription>
          {scope === "container"
            ? "Traffic through the backend container's own interfaces. The host machine's interfaces are not visible from inside the container."
            : "Network interfaces of the host machine."}{" "}
          {shown.length > 0 && `${up} of ${shown.length} up.`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {shown.length === 0 && <p className="text-sm text-muted-foreground">No network interface with traffic.</p>}
        <div className="space-y-3">
          {shown.map((iface) => {
            const prev = previous?.network.find((p) => p.name === iface.name);
            const rx = ratePerSecond(prev?.bytes_recv, iface.bytes_recv, previous?.timestamp, timestamp);
            const tx = ratePerSecond(prev?.bytes_sent, iface.bytes_sent, previous?.timestamp, timestamp);
            return (
              <div key={iface.name} className="p-3 bg-muted/30 rounded-lg space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-2">
                    {iface.is_up ? (
                      <Wifi className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                    ) : (
                      <WifiOff className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                    )}
                    <span className="font-medium font-mono">{iface.name}</span>
                  </div>
                  <Badge variant={iface.is_up ? "positive" : "muted"}>{iface.is_up ? "Up" : "Down"}</Badge>
                </div>
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <Direction icon={ArrowDownToLine} label="Receiving" rate={rx} total={iface.bytes_recv} since={since} />
                  <Direction icon={ArrowUpFromLine} label="Sending" rate={tx} total={iface.bytes_sent} since={since} />
                </div>
                {iface.addresses.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {iface.addresses.map((addr) => (
                      <Badge key={addr} variant="outline" className="font-mono text-xs">
                        {addr}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

function Direction({
  icon: Icon,
  label,
  rate,
  total,
  since,
}: {
  icon: React.ElementType;
  label: string;
  rate: number | null;
  total: number;
  since: string;
}) {
  const { formatBytes, formatNetworkRate } = useByteFormat();
  return (
    <div className="flex items-start gap-2">
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0">
        <p className="text-muted-foreground">{label}</p>
        <p className="font-medium tabular-nums">{rate !== null ? formatNetworkRate(rate) : <span className="font-normal text-muted-foreground">Measuring…</span>}</p>
        <p className="text-xs text-muted-foreground">
          {formatBytes(total)} {since}
        </p>
      </div>
    </div>
  );
}
