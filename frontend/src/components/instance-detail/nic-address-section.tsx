"use client";

/**
 * Per-interface IPv4 addressing on an existing instance.
 *
 * LXD's own web UI has no control for a fixed address, so
 * pinning one meant `lxc config device set` on the hypervisor over SSH. An operator on the NOC
 * desk has no business needing a root shell to give a database node the address it was planned
 * with.
 *
 * The interfaces are read from the expanded device set, because a NIC an instance inherits
 * from a profile is not in its own devices at all and is still the interface the operator sees.
 */

import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { DetailSection } from "@/components/detail/detail-field";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { useSetNicAddress } from "@/hooks/use-lxd";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useToast } from "@/hooks/use-toast";
import { Role } from "@/types/rbac";
import type { VMInfo } from "@/types/api";

interface Nic {
  device: string;
  network: string;
  address?: string;
  inherited: boolean;
}

function readNics(instance: VMInfo): Nic[] {
  const own = instance.devices ?? {};
  const expanded = instance.expanded_devices ?? {};
  const out: Nic[] = [];
  for (const [device, config] of Object.entries(expanded)) {
    if (config?.type !== "nic") continue;
    out.push({
      device,
      network: config.network || config.parent || "",
      address: config["ipv4.address"],
      inherited: !own[device],
    });
  }
  return out.sort((a, b) => a.device.localeCompare(b.device));
}

export function NicAddressSection({ instance }: { instance: VMInfo }) {
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const { toast } = useToast();
  const setAddress = useSetNicAddress();

  const canEdit = hasRole(Role.PARTNER);
  const nics = readNics(instance);

  const [editing, setEditing] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const inFlight = React.useRef(false);

  if (nics.length === 0) return null;

  const save = async (nic: Nic) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    try {
      const next = draft.trim();
      await setAddress.mutateAsync({
        instanceName: instance.name,
        deviceName: nic.device,
        host: instance.host,
        project: instance.project || "default",
        ipv4Address: next || null,
      });
      toast({
        title: next ? "Address reserved" : "Interface returned to DHCP",
        description: next
          ? `${nic.device} on ${instance.name} now uses ${next}.`
          : `${nic.device} on ${instance.name} will take an address from DHCP.`,
      });
      setEditing(null);
    } catch (e) {
      toast({
        title: "Could not change the address",
        description:
          e instanceof Error ? e.message : "The cluster refused the change.",
        variant: "destructive",
      });
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  };

  return (
    <DetailSection
      title="Interface Addressing"
      description="A reserved address survives a restart. An interface without one takes what DHCP offers."
    >
      <div className="space-y-2">
        {nics.map((nic) => {
          const isEditing = editing === nic.device;
          return (
            <div
              key={nic.device}
              className="flex flex-col gap-2 rounded-lg border p-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="min-w-0 space-y-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{nic.device}</span>
                  <Badge variant={nic.address ? "secondary" : "outline"}>
                    {nic.address ? "Static" : "DHCP"}
                  </Badge>
                  {nic.inherited && <Badge variant="outline">Inherited</Badge>}
                </div>
                <p className="text-xs text-muted-foreground">
                  {nic.network || EMPTY_VALUE}
                  {nic.address ? ` · ${nic.address}` : ""}
                </p>
              </div>

              {isEditing ? (
                <div className="flex flex-wrap items-end gap-2">
                  <div className="space-y-1">
                    <Label
                      htmlFor={`nic-addr-${nic.device}`}
                      className="text-xs text-muted-foreground"
                    >
                      IPv4 address
                    </Label>
                    <Input
                      id={`nic-addr-${nic.device}`}
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      placeholder="Empty for DHCP"
                      className="w-48"
                    />
                  </div>
                  <Button size="sm" onClick={() => save(nic)} disabled={saving}>
                    {saving ? "Saving..." : "Save"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setEditing(null)}
                    disabled={saving}
                  >
                    Cancel
                  </Button>
                </div>
              ) : (
                canEdit && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={isReadOnly}
                    title={
                      isReadOnly
                        ? "Action disabled in read-only mode"
                        : `Change the address on ${nic.device}`
                    }
                    onClick={() => {
                      setDraft(nic.address ?? "");
                      setEditing(nic.device);
                    }}
                  >
                    {nic.address ? "Change address" : "Reserve an address"}
                  </Button>
                )
              )}
            </div>
          );
        })}
      </div>
    </DetailSection>
  );
}
