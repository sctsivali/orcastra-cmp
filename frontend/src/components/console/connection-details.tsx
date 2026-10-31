/**
 * The graphical console's connection details: a compact chip (round trip, or frames when idle)
 * and a popover with the rest, so an operator can tell a slow link from a slow guest.
 */

"use client";

import * as React from "react";
import { SignalHigh, SignalLow, SignalMedium } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn, formatNetworkRate } from "@/lib/utils";
import type { SpiceStats, SpiceStatus } from "@/lib/spice/session";
import type { MouseMode } from "@/lib/spice/pointer";

interface ConnectionDetailsProps {
  status: SpiceStatus;
  stats: SpiceStats | null;
  mouseMode: MouseMode;
  agentConnected: boolean;
  resolution: { width: number; height: number } | null;
  scale: number | null;
}

// Round trips under 80 ms feel local; past 200 ms typing and pointing lag visibly.
function tone(rtt: number | null): { className: string; Icon: typeof SignalHigh; verdict: string } {
  if (rtt === null) return { className: "text-muted-foreground", Icon: SignalLow, verdict: "not measured yet" };
  if (rtt < 80) return { className: "text-status-online", Icon: SignalHigh, verdict: "good" };
  if (rtt < 200) return { className: "text-status-pending", Icon: SignalMedium, verdict: "fair" };
  return { className: "text-status-offline", Icon: SignalLow, verdict: "slow" };
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right tabular-nums">{value}</dd>
    </div>
  );
}

export function ConnectionDetails({ status, stats, mouseMode, agentConnected, resolution, scale }: ConnectionDetailsProps) {
  if (status !== "connected") return null;
  const rtt = stats?.rttMs ?? null;
  const { className, Icon, verdict } = tone(rtt);
  // The round trip is measured from pointer ACKs only (a fake move would keep the guest awake),
  // so before the first move the chip shows frames, and "Idle" on a still screen.
  const fps = stats?.fps ?? 0;
  const chip = rtt !== null ? `${rtt}ms` : fps > 0 ? `${fps} fps` : "Idle";

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm" className="h-8 gap-1.5 px-2 text-xs tabular-nums" aria-label={`Connection details: ${verdict}`}>
          {/* Bars only once there is a round trip to grade; an empty meter read as a stray dot. */}
          {rtt !== null && <Icon className={cn("h-4 w-4", className)} aria-hidden="true" />}
          {chip}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 text-sm">
        <p className="mb-1 font-medium">Connection</p>
        <dl className="divide-y">
          <Row
            label="Round Trip"
            value={rtt !== null ? `${rtt}ms (${verdict})` : "Move the pointer to measure"}
          />
          <Row label="Frames" value={`${stats?.fps ?? 0} per second`} />
          <Row label="Received" value={formatNetworkRate(stats?.bytesPerSecond ?? 0)} />
          <Row label="Pointer" value={mouseMode === "client" ? "Absolute" : "Relative (captured on click)"} />
          <Row label="Guest Agent" value={agentConnected ? "Running" : "Not running"} />
          {resolution && <Row label="Guest Resolution" value={`${resolution.width} × ${resolution.height}`} />}
          {scale !== null && <Row label="Shown at" value={`${Math.round(scale * 100)}%`} />}
        </dl>
        {!agentConnected && (
          <p className="mt-2 text-xs text-muted-foreground">
            Install spice-vdagent in the guest for clipboard sharing, file drops and a resolution that follows the window.
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}
