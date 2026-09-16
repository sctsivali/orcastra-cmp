"use client";

import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import type { PickerState } from "@/lib/picker-state";
import type { NicDevice } from "./devices";

interface NicRowProps {
  nic: NicDevice;
  networkOptions: SearchableSelectOption[];
  networkState: PickerState;
  /** The notice under the interfaces header that explains a failed network read. */
  networkNoticeId?: string;
  /** The chosen network's subnet, shown as the fixed-address placeholder. */
  subnetHint: string;
  addressError: string | null;
  onUpdate: (patch: Partial<NicDevice>) => void;
  onRemove: () => void;
}

/** One network interface: the managed network, the guest name, and an optional fixed IPv4. */
export function NicRow({
  nic,
  networkOptions,
  networkState,
  networkNoticeId,
  subnetHint,
  addressError,
  onUpdate,
  onRemove,
}: NicRowProps) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2 rounded-lg border p-3 items-end">
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground" htmlFor={`nic-network-${nic.id}`}>
          Network
        </Label>
        <SearchableSelect
          id={`nic-network-${nic.id}`}
          value={nic.network}
          onValueChange={(v) => onUpdate({ network: v })}
          options={networkOptions}
          placeholder={networkState.placeholder}
          searchPlaceholder="Search networks..."
          emptyText={networkState.emptyText}
          disabled={networkState.disabled}
          ariaDescribedBy={networkState.notice ? networkNoticeId : undefined}
          ariaInvalid={networkState.notice?.kind === "error"}
        />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground" htmlFor={`nic-name-${nic.id}`}>
          Interface name
        </Label>
        <Input
          id={`nic-name-${nic.id}`}
          value={nic.name}
          onChange={(e) => onUpdate({ name: e.target.value })}
          placeholder="eth0"
        />
      </div>
      <Button type="button" variant="ghost" size="icon" onClick={onRemove} aria-label="Remove network interface">
        <Trash2 className="h-4 w-4" />
      </Button>
      <div className="sm:col-span-3 space-y-2">
        <div className="flex items-center gap-2">
          <Checkbox
            id={`nic-static-${nic.id}`}
            checked={nic.ipv4Address !== undefined}
            onCheckedChange={(checked) => onUpdate({ ipv4Address: checked ? "" : undefined })}
          />
          <Label htmlFor={`nic-static-${nic.id}`} className="text-xs font-normal">
            Assign a fixed IPv4 address
          </Label>
        </div>
        {nic.ipv4Address !== undefined && (
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground" htmlFor={`nic-ipv4-${nic.id}`}>
              IPv4 address
            </Label>
            <Input
              id={`nic-ipv4-${nic.id}`}
              value={nic.ipv4Address}
              onChange={(e) => onUpdate({ ipv4Address: e.target.value })}
              placeholder={subnetHint}
              aria-invalid={!!addressError}
              aria-describedby={addressError ? `nic-ipv4-error-${nic.id}` : undefined}
            />
            {addressError ? (
              <p id={`nic-ipv4-error-${nic.id}`} className="text-xs text-destructive">
                {addressError}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">Leave empty to let DHCP choose.</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
