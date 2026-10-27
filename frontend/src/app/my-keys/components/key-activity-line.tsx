"use client";

import { Download, KeyRound, Server, ShieldAlert, Trash2, type LucideIcon } from "lucide-react";
import { formatDate } from "@/lib/utils";
import type { KeyActivityEntry } from "@/types/my-keys";

// note: `text`, not `label`: these are event descriptions read in a table cell, written as sentences.
const DASHBOARD: Record<string, { text: string; icon: LucideIcon }> = {
  "user.ssh_key.create": { text: "Key created", icon: KeyRound },
  "user.ssh_key.download": { text: "Private key downloaded", icon: Download },
  "user.ssh_key.delete": { text: "Key deleted", icon: Trash2 },
  "user.ssh_key.groups_cleanup": { text: "Orphaned key groups cleaned up", icon: Trash2 },
  "user.ssh_key.activity.export": { text: "Activity exported", icon: Download },
};

/** What a row says happened, in an operator's words. */
export function activityLabel(entry: KeyActivityEntry): string {
  if (entry.kind === "usage") return entry.message || entry.action || "Change on the cluster";
  const known = entry.action ? DASHBOARD[entry.action] : undefined;
  const base = known?.text ?? entry.action ?? "Key action";
  if (entry.result === "denied") return `${base}: refused`;
  if (entry.result === "failure") return `${base}: failed`;
  return base;
}

/** Where the client address came from, said plainly: a proxy's own address is not the browser's. */
export function clientText(entry: KeyActivityEntry): string | null {
  const client = entry.client;
  if (!client?.ip) return null;
  const forwarded = client.ip_source && client.ip_source !== "direct";
  const where = forwarded
    ? client.ip
    : client.proxy_trusted
      ? `${client.ip} (client address not forwarded)`
      : client.ip;
  return client.user_agent ? `${where}, ${client.user_agent}` : where;
}

export function KeyActivityLine({ entry }: { entry: KeyActivityEntry }) {
  const known = entry.action ? DASHBOARD[entry.action] : undefined;
  const Icon = entry.kind === "usage" ? Server : entry.result === "denied" ? ShieldAlert : known?.icon ?? KeyRound;
  const where = [entry.cluster_id, entry.project].filter(Boolean).join(" / ");
  const client = clientText(entry);
  return (
    <div className="flex min-w-0 gap-3 text-sm" data-kind={entry.kind} data-action={entry.action ?? undefined}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 space-y-0.5">
        <p className="font-medium break-words">{activityLabel(entry)}</p>
        <p className="text-xs text-muted-foreground break-words">
          {formatDate(entry.ts)}
          {where ? ` · ${where}` : ""}
          {entry.kind === "usage" ? " · with this key's certificate" : entry.actor_email ? ` · ${entry.actor_email}` : ""}
        </p>
        {client && <p className="text-xs text-muted-foreground break-all">{client}</p>}
      </div>
    </div>
  );
}
