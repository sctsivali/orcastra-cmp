"use client";

import { Badge } from "@/components/ui/badge";
import { OverflowCell } from "@/components/ui/overflow-cell";
import { RowActions } from "@/components/ui/row-actions";
import { TableCell, TableRow } from "@/components/ui/table";
import { NodeLink } from "@/components/ui/node-link";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TagBadge } from "@/components/tag-manager";
import { useConsoleStore } from "@/lib/console-store";
import { useTerminalStore } from "@/lib/terminal-store";
import { formatInstanceType } from "@/lib/utils";
import type { VMInfo } from "@/types/api";
import { instanceRowActions, type InstancePermission } from "./instance-row-actions";
import type { InstanceAction } from "./use-instance-actions";

export interface InstanceTableRowProps {
  instance: VMInfo;
  /** From useRowFocus: the row key and, briefly after a create, the mark. */
  rowProps: { "data-row-key": string; "data-row-focus"?: "true" | "false" };
  onOpen: (instance: VMInfo) => void;
  onAction: (name: string, host: string, project: string, action: InstanceAction) => void;
  actionLoading: string | null;
  isReadOnly: boolean;
  /** Undefined when the caller may act; otherwise why not, including "still checking". */
  permissionReason: (
    host: string,
    project: string,
    name: string,
    permission: InstancePermission,
  ) => string | undefined;
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
function hasFixedAddress(instance: Pick<VMInfo, "devices" | "expanded_devices">): boolean {
  const sets = [instance.expanded_devices, instance.devices];
  for (const devices of sets) {
    for (const device of Object.values(devices ?? {})) {
      if (device?.type === "nic" && device["ipv4.address"]) return true;
    }
  }
  return false;
}

// The root disk an instance actually runs on. LXD keeps a size chosen at create time on the
// instance's own device; an instance that overrode nothing still has one through its profile,
// and in that case LXD reports no size because the pool default applies.
function rootDiskLabel(
  instance: Pick<VMInfo, "devices" | "expanded_devices">,
): { value: string; detail?: string } | null {
  const own = instance.devices?.root;
  const effective = instance.expanded_devices?.root ?? own;
  if (!effective) return null;
  const size = String(effective.size ?? "").trim();
  const pool = String(effective.pool ?? "").trim();
  if (!size) return { value: "Pool default", detail: pool ? `Pool ${pool}` : undefined };
  const origin = own?.size ? "Set on instance" : "From profile";
  return { value: size, detail: pool ? `${origin}, pool ${pool}` : origin };
}

const IN_FLIGHT: Record<InstanceAction, string> = {
  start: "Starting",
  stop: "Stopping",
  restart: "Restarting",
};

function inFlightLabel(name: string, actionLoading: string | null, deletePending: boolean) {
  if (deletePending) return "Deleting";
  for (const action of Object.keys(IN_FLIGHT) as InstanceAction[]) {
    if (actionLoading === `${name}-${action}`) return IN_FLIGHT[action];
  }
  return null;
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
  permissionReason,
  canDeleteInstance,
  deletePending,
  onCopy,
  onRebuild,
  onDelete,
}: InstanceTableRowProps) {
  const project = instance.project || "default";
  // An instance name is unique only within one project on one host, and this table spans
  // both, so the trigger names all three. Same shape as components/dashboard/instance-row.tsx.
  const rowLabel = `${instance.name} in ${project} on ${instance.host}`;
  const rootDisk = rootDiskLabel(instance);
  const inFlight = inFlightLabel(instance.name, actionLoading, deletePending);

  const actions = instanceRowActions({
    instance,
    isReadOnly,
    canManage: canDeleteInstance,
    actionLoading,
    deletePending,
    permissionReason: (permission) => permissionReason(instance.host, project, instance.name, permission),
    onStart: () => onAction(instance.name, instance.host, project, "start"),
    onStop: () => onAction(instance.name, instance.host, project, "stop"),
    onRestart: () => onAction(instance.name, instance.host, project, "restart"),
    onTerminal: () =>
      useTerminalStore.getState().openTerminal({
        instanceName: instance.name,
        host: instance.host,
        project,
      }),
    onConsole: () =>
      useConsoleStore.getState().openConsole({
        instanceName: instance.name,
        instanceType: instance.type || "container",
        host: instance.host,
        project,
      }),
    onCopy: () => onCopy(instance),
    onRebuild: () => onRebuild(instance),
    onDelete: () => onDelete(instance),
  });

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
        {/* A closed menu hides the item's spinner, so the row itself reports what is running. */}
        {inFlight && <TableSubText>{inFlight}</TableSubText>}
      </TableCell>
      <TableCell>{formatInstanceType(instance.type)}</TableCell>
      <TableCell className="whitespace-nowrap">
        <NodeLink clusterId={instance.host} />
      </TableCell>
      <TableCell className="whitespace-nowrap">{project}</TableCell>
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
      <TableCell className="whitespace-nowrap">
        {rootDisk ? (
          <>
            {rootDisk.value}
            {rootDisk.detail && <TableSubText>{rootDisk.detail}</TableSubText>}
          </>
        ) : (
          <TableEmptyValue label="No root disk reported" />
        )}
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
      <TableCell className="text-right">
        <RowActions triggerLabel={`Actions for instance ${rowLabel}`} actions={actions} />
      </TableCell>
    </TableRow>
  );
}
