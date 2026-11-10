"use client";

import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Flame,
  Info,
  Shield,
  ShieldAlert,
  ShieldX,
  WifiOff,
  type LucideIcon,
} from "lucide-react";

// Every tile opens a list page with its own query string, a distinct router cache key, so tiles
// warm on intent like the sidebar instead of costing an RSC request per tile on every paint.
import { IntentLink as Link } from "@/components/navigation/route-intent";
import { TONE_TEXT, type ClusterStatusTone } from "@/lib/cluster-status";
import { cn } from "@/lib/utils";

export type AlarmKind =
  | "total"
  | "critical"
  | "high"
  | "warning"
  | "info"
  | "certificates"
  | "offlineClusters"
  | "kernel";

// The labels and tones are the Warnings page's own (warning-severity.ts and its tab titles), so
// a tile reads the same word in the same tone as the list it opens.
const ALARM_KINDS: Record<AlarmKind, { label: string; icon: LucideIcon; tone: ClusterStatusTone }> = {
  total: { label: "Total Warnings", icon: Bell, tone: "neutral" },
  critical: { label: "Critical", icon: Flame, tone: "negative" },
  high: { label: "High", icon: ShieldAlert, tone: "negative" },
  warning: { label: "Warning", icon: AlertTriangle, tone: "caution" },
  info: { label: "Info", icon: Info, tone: "neutral" },
  certificates: { label: "Cert Issues", icon: Shield, tone: "caution" },
  offlineClusters: { label: "Offline Clusters", icon: WifiOff, tone: "negative" },
  kernel: { label: "Kernel CVEs", icon: ShieldX, tone: "negative" },
};

/**
 * One alarm count that opens its list. Shared by the dashboard's System Alerts and the
 * Monitoring page's Alarm Statistics.
 *
 * A neutral tile: the colour is on the icon alone, and only while the count is above zero, so a
 * fleet with nothing wrong paints no row of alarm colours and the tile that matters stands out.
 *
 * Browser tests read the number from the first <p> in the link; keep it first.
 */
function AlarmTileBody({ kind, count }: { kind: AlarmKind; count: number }) {
  const { label, icon: Icon, tone } = ALARM_KINDS[kind];
  const active = count > 0;
  return (
    <>
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-muted">
        <Icon className={cn("h-4 w-4", active ? TONE_TEXT[tone] : "text-muted-foreground")} aria-hidden="true" />
      </div>
      <div className="min-w-0">
        <p className={cn("text-xl font-bold tabular-nums", active ? "text-foreground" : "text-muted-foreground")}>
          {count}
        </p>
        <p className="text-xs text-muted-foreground">{label}</p>
      </div>
    </>
  );
}

export function AlarmTile({ kind, count, href }: { kind: AlarmKind; count: number; href: string }) {
  return (
    <Link
      href={href}
      className={cn(
        "flex items-center gap-3 rounded-lg border bg-card p-3 transition-colors hover:bg-muted/50",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-background motion-reduce:transition-none",
      )}
    >
      <AlarmTileBody kind={kind} count={count} />
    </Link>
  );
}

/** The same count where the list it would open is already on the page, as on Warnings. */
export function AlarmCount({ kind, count }: { kind: AlarmKind; count: number }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border bg-card p-3">
      <AlarmTileBody kind={kind} count={count} />
    </div>
  );
}

/** The line a card shows when every tile reads zero: plain text, no banner. */
export function NoActiveAlarms() {
  return (
    <p className="flex items-center gap-2 text-sm text-muted-foreground">
      <CheckCircle2 className={cn("h-4 w-4 shrink-0", TONE_TEXT.positive)} aria-hidden="true" />
      No active alarms
    </p>
  );
}
