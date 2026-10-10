import { EMPTY_VALUE } from "@/components/ui/table-text";
import type { CertificateInfo } from "@/types/api";

export function getDaysDisplay(days: number | null | undefined): string {
  if (days === null || days === undefined) return EMPTY_VALUE;
  if (days < 0) return `Expired ${Math.abs(days)} days ago`;
  if (days === 0) return "Expires today";
  if (days === 1) return "Expires tomorrow";
  return `${days} days remaining`;
}

export function daysTone(days: number | null | undefined): string {
  if (days === null || days === undefined) return "text-muted-foreground";
  if (days <= 0) return "text-status-offline";
  if (days <= 14) return "text-status-pending";
  return "text-status-online";
}

export function certToTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : t;
}

/** Superseded trust entries still on the cluster: waiting out the grace period, or refused. */
export function openSuperseded(cert: CertificateInfo) {
  return (cert.superseded ?? []).filter((entry) => entry.state === "pending" || entry.state === "failed");
}
