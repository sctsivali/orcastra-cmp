"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { DetailField, DetailGrid } from "@/components/detail/detail-field";
import { formatBytes, formatFrequency } from "@/lib/utils";
import type { CPUSocketInfo, StorageDeviceInfo } from "@/types/api";

/**
 * Sockets and disks as description lists.
 *
 * Deliberately not tables. check-table-filters.mjs is at its ratchet, so a TableBody outside
 * components/ui without a defineTable contract fails the frontend gate, and config-panel.tsx
 * already records the house answer: a dl, not a Table.
 *
 * Nothing here re-cases a value. A product name, a driver name and an architecture are all
 * registry-authored, and formatLabel turns x86_64 into "X86 64".
 */

/** One bordered row inside a section. Sections may hold several of these. */
function DeviceRow({
  title,
  subtitle,
  badges,
  children,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  badges?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="min-w-0">
          <p className="text-sm font-medium break-words">{title}</p>
          {subtitle ? <p className="text-xs text-muted-foreground break-words">{subtitle}</p> : null}
        </div>
        {badges ? <div className="flex flex-wrap gap-1.5">{badges}</div> : null}
      </div>
      {children}
    </div>
  );
}

function MemberBadge({ member }: { member?: string | null }) {
  // Only meaningful on a cluster fold, where a device belongs to exactly one host.
  if (!member) return null;
  return (
    <Badge variant="outline" className="font-normal">
      {member}
    </Badge>
  );
}

export function CpuSocketList({ sockets }: { sockets: CPUSocketInfo[] }) {
  if (sockets.length === 0) return null;
  return (
    <div className="space-y-2">
      {sockets.map((socket, index) => (
        <DeviceRow
          key={`${socket.member ?? ""}-${socket.socket ?? index}`}
          title={socket.name || "Processor"}
          subtitle={socket.vendor || undefined}
          badges={
            <>
              {socket.socket !== null && socket.socket !== undefined ? (
                <Badge variant="outline" className="font-normal">
                  Socket {socket.socket}
                </Badge>
              ) : null}
              <MemberBadge member={socket.member} />
            </>
          }
        >
          <DetailGrid cols={4}>
            <DetailField label="Cores" value={socket.cores || undefined} />
            <DetailField label="Threads" value={socket.threads || undefined} />
            <DetailField label="Base Clock" value={formatFrequency(socket.frequency)} />
            <DetailField label="Turbo" value={formatFrequency(socket.frequency_turbo)} />
          </DetailGrid>
        </DeviceRow>
      ))}
    </div>
  );
}

export function StorageDeviceList({ devices }: { devices: StorageDeviceInfo[] }) {
  if (devices.length === 0) return null;
  return (
    <div className="space-y-2">
      {devices.map((disk, index) => (
        <DeviceRow
          key={`${disk.member ?? ""}-${disk.id ?? index}`}
          title={disk.model || disk.id || "Disk"}
          subtitle={disk.id && disk.model ? disk.id : undefined}
          badges={
            <>
              {disk.type ? (
                <Badge variant="outline" className="font-normal uppercase">
                  {disk.type}
                </Badge>
              ) : null}
              {disk.read_only ? <Badge variant="caution">Read only</Badge> : null}
              {disk.removable ? (
                <Badge variant="outline" className="font-normal">
                  Removable
                </Badge>
              ) : null}
              <MemberBadge member={disk.member} />
            </>
          }
        >
          <DetailGrid cols={4}>
            <DetailField label="Capacity" value={disk.size ? formatBytes(disk.size) : undefined} />
            {/* note: rpm, not a boolean. 0 is the node saying solid state, which is a fact worth
                printing rather than a missing value. */}
            <DetailField
              label="Rotation"
              value={
                disk.rpm === null || disk.rpm === undefined
                  ? undefined
                  : disk.rpm > 0
                    ? `${disk.rpm} rpm`
                    : "Solid state"
              }
            />
            <DetailField label="Firmware" value={disk.firmware_version || undefined} mono />
            <DetailField
              label="Block Size"
              value={disk.block_size ? `${disk.block_size} B` : undefined}
            />
            <DetailField label="Partitions" value={disk.partitions || undefined} />
            <DetailField
              label="NUMA Node"
              value={disk.numa_node === null || disk.numa_node === undefined ? undefined : disk.numa_node}
            />
          </DetailGrid>
        </DeviceRow>
      ))}
    </div>
  );
}
