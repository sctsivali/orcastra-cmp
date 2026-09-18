"use client";

import { Copy, Monitor, Play, RefreshCw, RotateCw, Square, Terminal, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { TableCell, TableRow } from "@/components/ui/table";
import { NodeLink } from "@/components/ui/node-link";
import { TableLink } from "@/components/ui/table-link";
import { EMPTY_VALUE, TableSubText } from "@/components/ui/table-text";
import { TagBadge } from "@/components/tag-manager";
import { useConsoleStore } from "@/lib/console-store";
import { useTerminalStore } from "@/lib/terminal-store";
import { formatInstanceType } from "@/lib/utils";
import type { VMInfo } from "@/types/api";

type Permission = "view" | "operate" | "console";
type RowAction = "start" | "stop" | "restart";

export interface InstanceTableRowProps {
  instance: VMInfo;
  /** From useRowFocus: the row key and, briefly after a create, the mark. */
  rowProps: { "data-row-key": string; "data-row-focus"?: "true" | "false" };
  onOpen: (instance: VMInfo) => void;
  onAction: (name: string, host: string, project: string, action: RowAction) => void;
  actionLoading: string | null;
  isReadOnly: boolean;
  hasPermission: (host: string, project: string, name: string, permission: Permission) => boolean;
  permissionTitle: (
    host: string,
    project: string,
    name: string,
    permission: Permission,
    allowed: string,
    denied: string,
  ) => string;
  canDeleteInstance: boolean;
  /** True only for the row whose delete is in flight, so the other rows stay live. */
  deletePending: boolean;
  onCopy: (instance: VMInfo) => void;
  onRebuild: (instance: VMInfo) => void;
  onDelete: (instance: VMInfo) => void;
}

/**
 * Whether any NIC on this instance pins its own IPv4 rather than taking one from DHCP.
 *
 * Reads the instance's own devices and its expanded set, because the NIC usually comes from a
 * profile and only appears in the expanded view. The case that matters: an operator
 * reconciling against a corporate IPAM catalogue needs to see which addresses are planned and
 * which are merely current.
 */
function hasFixedAddress(instance: {
  devices?: Record<string, Record<string, string>> | null;
  expanded_devices?: Record<string, Record<string, string>> | null;
}): boolean {
  const sets = [instance.expanded_devices, instance.devices];
  for (const devices of sets) {
    for (const device of Object.values(devices ?? {})) {
      if (device?.type === "nic" && device["ipv4.address"]) return true;
    }
  }
  return false;
}

function rootDiskLabel(instance: any): { text: string; title: string } {
  const own = instance?.devices?.root;
  const effective = instance?.expanded_devices?.root ?? own;
  if (!effective) return { text: EMPTY_VALUE, title: "No root disk device reported by LXD" };
  const size = String(effective.size ?? "").trim();
  const pool = String(effective.pool ?? "").trim();
  if (!size) {
    return {
      text: pool ? `pool default (${pool})` : "pool default",
      title: "No explicit size on the root disk, so the storage pool default applies",
    };
  }
  const overridden = Boolean(own?.size);
  return {
    text: size,
    title: overridden
      ? `Set on this instance${pool ? `, pool ${pool}` : ""}`
      : `Inherited from a profile${pool ? `, pool ${pool}` : ""}`,
  };
}

function getStatusColor(status: string) {
  switch (status.toLowerCase()) {
    case "running":
      return "default";
    case "stopped":
      return "secondary";
    case "frozen":
      return "outline";
    default:
      return "destructive";
  }
}

export function InstanceTableRow({
  instance,
  rowProps,
  onOpen,
  onAction,
  actionLoading,
  isReadOnly,
  hasPermission,
  permissionTitle,
  canDeleteInstance,
  deletePending,
  onCopy,
  onRebuild,
  onDelete,
}: InstanceTableRowProps) {
  return (
    <TableRow {...rowProps}>
      <TableCell className="font-medium">
        <TableLink onClick={() => onOpen(instance)}>
          {instance.name}
        </TableLink>
      </TableCell>
      <TableCell>
        <Badge variant={getStatusColor(instance.status)}>
          {instance.status}
        </Badge>
      </TableCell>
      <TableCell>{formatInstanceType(instance.type)}</TableCell>
      <TableCell className="whitespace-nowrap">
        <NodeLink clusterId={instance.host} />
      </TableCell>
      <TableCell className="whitespace-nowrap">{instance.project || "default"}</TableCell>
      <TableCell>
        <OverflowCell
          items={instance.ip_addresses}
          preview={2}
          noun="IP addresses"
          ownerLabel={instance.name}
          emptyLabel="No IP addresses"
          itemLabel="IP address"
        />
        {/* The allocation label operators asked for. It must not be
            gated on a live address: ip_addresses is populated only for a running
            instance, so gating on it hid the label in exactly the case the label
            exists for, a stopped instance whose address is pinned and which an
            operator is reconciling against an IPAM catalogue. A pinned address is
            a property of the device, not of the instance being up. */}
        {hasFixedAddress(instance) ? (
          <TableSubText>Static</TableSubText>
        ) : instance.ip_addresses?.length ? (
          <TableSubText>DHCP</TableSubText>
        ) : null}
      </TableCell>
      <TableCell>
        <OverflowCell
          items={instance.profiles}
          preview={3}
          noun="profiles"
          ownerLabel={instance.name}
          emptyLabel="No profiles"
          itemLabel="profile"
        />
      </TableCell>
      <TableCell className="whitespace-nowrap" title={rootDiskLabel(instance).title}>
        {rootDiskLabel(instance).text}
      </TableCell>
      <TableCell>
        <OverflowCell
          items={instance.tags}
          preview={3}
          noun="tags"
          ownerLabel={instance.name}
          emptyLabel="No tags"
          triggerMode="indicator"
          separator={null}
          renderInline={(tag) => <TagBadge tag={tag as string} size="sm" />}
          renderItem={(tag) => <TagBadge tag={tag as string} size="sm" />}
        />
      </TableCell>
      <TableCell>
        <div className="flex gap-2">
          {instance.status !== "Running" && (
            <Button
              size="sm"
              variant="outline"
              onClick={() =>
                onAction(instance.name, instance.host, instance.project || "default", "start")
              }
              disabled={actionLoading === `${instance.name}-start` || isReadOnly || !hasPermission(instance.host, instance.project || "default", instance.name, "operate")}
              aria-label={`Start instance ${instance.name}`}
              title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Start instance", "No operate permission")}
            >
              <Play className="h-4 w-4" />
            </Button>
          )}
          {instance.status === "Running" && (
            <>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  onAction(instance.name, instance.host, instance.project || "default", "stop")
                }
                disabled={actionLoading === `${instance.name}-stop` || isReadOnly || !hasPermission(instance.host, instance.project || "default", instance.name, "operate")}
                aria-label={`Stop instance ${instance.name}`}
                title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Stop instance", "No operate permission")}
              >
                <Square className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  onAction(instance.name, instance.host, instance.project || "default", "restart")
                }
                disabled={actionLoading === `${instance.name}-restart` || isReadOnly || !hasPermission(instance.host, instance.project || "default", instance.name, "operate")}
                aria-label={`Restart instance ${instance.name}`}
                title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Restart instance", "No operate permission")}
              >
                <RotateCw className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  useTerminalStore.getState().openTerminal({
                    instanceName: instance.name,
                    host: instance.host,
                    project: instance.project || "default",
                  });
                }}
                disabled={!hasPermission(instance.host, instance.project || "default", instance.name, "console")}
                aria-label={`Open terminal for instance ${instance.name}`}
                title={permissionTitle(instance.host, instance.project || "default", instance.name, "console", "Open Terminal", "No console permission")}
              >
                <Terminal className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  useConsoleStore.getState().openConsole({
                    instanceName: instance.name,
                    instanceType: instance.type || "container",
                    host: instance.host,
                    project: instance.project || "default",
                  });
                }}
                disabled={!hasPermission(instance.host, instance.project || "default", instance.name, "console")}
                aria-label={`Open console for instance ${instance.name}`}
                title={permissionTitle(instance.host, instance.project || "default", instance.name, "console", "Open Console", "No console permission")}
              >
                <Monitor className="h-4 w-4" />
              </Button>
            </>
          )}
          {canDeleteInstance && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onCopy(instance)}
              disabled={
                isReadOnly ||
                !hasPermission(instance.host, instance.project || "default", instance.name, "operate")
              }
              aria-label={`Copy instance ${instance.name} elsewhere`}
              title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Copy this instance to another project or cluster", "No operate permission")}
            >
              <Copy className="h-4 w-4" />
            </Button>
          )}
          {canDeleteInstance && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onRebuild(instance)}
              disabled={
                isReadOnly ||
                !hasPermission(instance.host, instance.project || "default", instance.name, "operate")
              }
              aria-label={`Rebuild instance ${instance.name}`}
              title={permissionTitle(instance.host, instance.project || "default", instance.name, "operate", isReadOnly ? "Action disabled in read-only mode" : "Rebuild instance (reinstall root disk)", "No operate permission")}
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
          )}
          {canDeleteInstance && (
            <Button
              size="sm"
              variant="outline"
              className="text-destructive hover:text-destructive"
              onClick={() => onDelete(instance)}
              // Only the row actually being deleted greys out; a
              // page-wide isPending would disable every row's button.
              disabled={isReadOnly || deletePending}
              aria-label={`Delete instance ${instance.name}`}
              title={isReadOnly ? "Action disabled in read-only mode" : "Delete instance"}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}
