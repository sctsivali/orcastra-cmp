"use client";

import { TableEmptyValue } from "@/components/ui/table-text";
import type { SnapshotInfo } from "@/types/api";

/**
 * The cluster and project a snapshot actually lives on.
 *
 * important: never the page's scope. The listing spans clusters and projects, so a page-level
 * host deletes or restores against the wrong one. `host` is optional on the type because the
 * per-instance listing does not carry it; the fleet route sets it on every row.
 */
export function snapshotScope(snapshot: SnapshotInfo): { host: string; project: string } {
  return { host: snapshot.host ?? "", project: snapshot.project || "default" };
}

/** Whole units only. A snapshot's age is read to decide whether it is still useful, not to the second. */
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

/** Expiry as time remaining.
 *
 * A missing expiry is stated plainly rather than left blank: on this surface it means the
 * snapshot belongs to the metered product upstream, which keeps it until its own retention says
 * otherwise. Nothing this console creates is ever without one.
 */
export function expiryLabel(iso?: string | null) {
  if (!iso) return <TableEmptyValue label="No expiry" />;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return <TableEmptyValue label="No expiry" />;
  const seconds = Math.floor((at - Date.now()) / 1000);
  if (seconds <= 0) return <span className="text-muted-foreground">due to be pruned</span>;
  const hours = Math.floor(seconds / 3600);
  if (hours < 1) return `in ${Math.max(1, Math.floor(seconds / 60))}m`;
  if (hours < 48) return `in ${hours}h`;
  return `in ${Math.floor(hours / 24)}d`;
}
