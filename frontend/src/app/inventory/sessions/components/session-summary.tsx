"use client";

import type { ComponentType, ReactNode } from "react";
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Clock,
  History,
  Wifi,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { IllustrationFallback, type IllustrationProps } from "@/components/illustrations/illustration-frame";
import { DownloadIllustration, DurationIllustration, SessionsActiveIllustration, UploadIllustration } from "@/components/illustrations/scenes/kpi";
import { useByteFormat } from "@/hooks/use-byte-format";
import { EMPTY_VALUE, formatCount, formatElapsed } from "@/lib/utils";

/**
 * One counter above a Sessions table, in the house stat style of the Regions tile: a small icon
 * beside a `text-sm` label over a `text-2xl font-bold` value in the body face, with tabular
 * figures so the 5 s rate poll does not shift the digits. The icon sits in the label row because
 * beside the value it would take the room a rate like `999.9 MB/s` needs. Label and value stay
 * sibling paragraphs: an end-to-end spec reads the count as the label's next `p`.
 */
function SummaryTile({
  label,
  value,
  icon: Icon,
  illustration: Scene,
}: {
  label: string;
  value: ReactNode;
  icon: LucideIcon;
  illustration: ComponentType<IllustrationProps>;
}) {
  // The scene shows only from a 300px tile (illustrations.css, illo-stat), where the value still
  // keeps the 135 px a rate needs beside it; below that the label's icon returns.
  return (
    <Card className="illo-stat">
      <CardContent className="flex items-start justify-between gap-4 pt-4">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <IllustrationFallback>
              <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
            </IllustrationFallback>
            {label}
          </p>
          <p className="mt-1 text-2xl font-bold tabular-nums">{value}</p>
        </div>
        <Scene size="sm" />
      </CardContent>
    </Card>
  );
}

// The sidebar takes 256 px from md up, so the content is narrower at 768 px than at 640 px.
// At 24 px bold Geist the widest values are `999.9 MB/s` (135 px) live and `999.99 GB` (123 px) in
// history, plus 48 px of tile padding. The live tiles use auto-fit, which measures the real
// container; 12rem holds 135 px. The history tiles keep fixed breakpoints (1, 2, 4), because
// auto-fit would leave three and one at 1024 px.
const ACTIVE_STRIP = "grid gap-4 grid-cols-[repeat(auto-fit,minmax(12rem,1fr))]";
const HISTORY_STRIP = "grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4";

/** The live strip. `countUnknown`: nothing has been read for this actor and scope yet. */
export function ActiveSummary({
  countUnknown,
  count,
  downloadRate,
  uploadRate,
}: {
  countUnknown: boolean;
  count: number;
  downloadRate: number;
  uploadRate: number;
}) {
  const { formatNetworkRate } = useByteFormat();
  const shown = (text: string) => (countUnknown ? EMPTY_VALUE : text);
  return (
    <div className={ACTIVE_STRIP}>
      <SummaryTile label="Active" value={shown(formatCount(count))} icon={Wifi} illustration={SessionsActiveIllustration} />
      <SummaryTile label="Download" value={shown(formatNetworkRate(downloadRate, 1))} icon={ArrowDownToLine} illustration={DownloadIllustration} />
      <SummaryTile label="Upload" value={shown(formatNetworkRate(uploadRate, 1))} icon={ArrowUpFromLine} illustration={UploadIllustration} />
    </div>
  );
}

/**
 * The history strip, summed over the disconnect rows that were read. A read that has not landed
 * (or failed with nothing held) shows the placeholder rather than an unmeasured `0` or `0 B`.
 */
export function HistorySummary({
  countUnknown,
  sessions,
  durationSeconds,
  downloadBytes,
  uploadBytes,
}: {
  countUnknown: boolean;
  sessions: number;
  durationSeconds: number;
  downloadBytes: number;
  uploadBytes: number;
}) {
  const { formatBytes } = useByteFormat();
  const shown = (text: string) => (countUnknown ? EMPTY_VALUE : text);
  return (
    <div className={HISTORY_STRIP}>
      <SummaryTile label="Past Sessions" value={shown(formatCount(sessions))} icon={History} illustration={SessionsActiveIllustration} />
      <SummaryTile label="Total Time" value={shown(formatElapsed(durationSeconds))} icon={Clock} illustration={DurationIllustration} />
      <SummaryTile label="Downloaded" value={shown(formatBytes(downloadBytes))} icon={ArrowDownToLine} illustration={DownloadIllustration} />
      <SummaryTile label="Uploaded" value={shown(formatBytes(uploadBytes))} icon={ArrowUpFromLine} illustration={UploadIllustration} />
    </div>
  );
}
