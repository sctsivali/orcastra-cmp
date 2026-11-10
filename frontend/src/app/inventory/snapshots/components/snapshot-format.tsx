"use client";

import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { formatDateTime } from "@/lib/utils";
import type { SnapshotInfo } from "@/types/api";

/**
 * The cluster and project a snapshot lives on.
 *
 * important: never the page's scope. The listing spans clusters and projects, so a page-level
 * host deletes or restores against the wrong one. `host` is optional on the type because the
 * per-instance listing does not carry it; the fleet route sets it on every row.
 */
export function snapshotScope(snapshot: SnapshotInfo): { host: string; project: string } {
  return { host: snapshot.host ?? "", project: snapshot.project || "default" };
}

/** Age in whole units; a snapshot's age is read to judge whether it is still useful. */
export function relativeAge(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  return months < 12 ? `${months}mo ago` : `${Math.floor(months / 12)}y ago`;
}

/**
 * Time remaining, in the same whole units `relativeAge` reads back.
 *
 * Shared by a snapshot's expiry and a schedule's next run so "in 6h" has one spelling on screen;
 * only the meaning of a past instant differs, so that is the parameter. Returns a string so prose
 * callers and table cells share the spelling.
 */
export function timeUntilLabel(iso: string, pastLabel: string): string {
  const seconds = Math.floor((Date.parse(iso) - Date.now()) / 1000);
  if (seconds <= 0) return pastLabel;
  const hours = Math.floor(seconds / 3600);
  if (hours < 1) return `in ${Math.max(1, Math.floor(seconds / 60))}m`;
  if (hours < 48) return `in ${hours}h`;
  return `in ${Math.floor(hours / 24)}d`;
}

/** `timeUntilLabel` for prose, with an absence and a past instant each named by the caller. */
export function timeUntil(
  iso: string | null | undefined,
  { emptyLabel, pastLabel }: { emptyLabel: string; pastLabel: string },
): React.ReactNode {
  if (!iso || Number.isNaN(Date.parse(iso))) return <TableEmptyValue label={emptyLabel} />;
  const label = timeUntilLabel(iso, pastLabel);
  return label === pastLabel ? <span className="text-muted-foreground">{label}</span> : label;
}

/**
 * Expiry as time remaining.
 *
 * A missing expiry is stated rather than left blank: every snapshot this console creates has
 * one, so here it means the snapshot belongs to the metered product upstream, which keeps it
 * under its own retention.
 */
export function expiryLabel(iso?: string | null): React.ReactNode {
  return timeUntil(iso, { emptyLabel: "No expiry", pastLabel: "due to be pruned" });
}

/**
 * When a schedule fires next.
 *
 * A missing next run means the schedule is paused, and a next run in the past means the tick has
 * not reached it yet; neither means anything is being deleted, so the labels differ from
 * `expiryLabel`.
 */
export function nextRunLabel(iso?: string | null): React.ReactNode {
  return timeUntil(iso, { emptyLabel: "Paused", pastLabel: "due now" });
}

/**
 * A timestamp cell: the absolute instant as the value, the relative reading beneath it.
 *
 * The absolute stamp is compared against a change window or a log line; the age or countdown is
 * scanned. Shared by the fleet table, the per-instance tab and the schedules table.
 */
export function AbsoluteWithRelative({
  iso,
  emptyLabel,
  relative,
}: {
  iso?: string | null;
  emptyLabel: string;
  relative: (iso: string) => string;
}) {
  if (!iso || Number.isNaN(Date.parse(iso))) return <TableEmptyValue label={emptyLabel} />;
  return (
    <>
      <time dateTime={iso}>{formatDateTime(iso)}</time>
      <TableSubText>{relative(iso)}</TableSubText>
    </>
  );
}

export function createdCell(iso?: string | null): React.ReactNode {
  return <AbsoluteWithRelative iso={iso} emptyLabel="Unknown" relative={relativeAge} />;
}

export function expiryCell(iso?: string | null): React.ReactNode {
  return (
    <AbsoluteWithRelative
      iso={iso}
      emptyLabel="No expiry"
      relative={(at) => timeUntilLabel(at, "due to be pruned")}
    />
  );
}

export function nextRunCell(iso?: string | null): React.ReactNode {
  return (
    <AbsoluteWithRelative
      iso={iso}
      emptyLabel="Paused"
      relative={(at) => timeUntilLabel(at, "due now")}
    />
  );
}

export function lastRunCell(iso?: string | null): React.ReactNode {
  return <AbsoluteWithRelative iso={iso} emptyLabel="Never run" relative={relativeAge} />;
}
