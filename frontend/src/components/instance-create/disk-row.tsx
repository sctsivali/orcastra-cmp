"use client";

import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import type { PickerState } from "@/lib/picker-state";
import type { DiskDevice } from "./devices";

interface DiskRowProps {
  disk: DiskDevice;
  instanceType: "vm" | "container";
  poolOptions: SearchableSelectOption[];
  poolState: PickerState;
  volumeOptions: SearchableSelectOption[];
  volumeState: PickerState;
  /** A block volume attaches as a raw device and takes no mount path. */
  isBlock: boolean;
  /** The notice under the disks header that explains a failed volume read. */
  volumeNoticeId?: string;
  allowHostPath: boolean;
  onUpdate: (patch: Partial<DiskDevice>) => void;
  onRemove: () => void;
}

/** One additional disk: a custom volume (pool, then volume) or a host path, plus its mount. */
export function DiskRow({
  disk,
  instanceType,
  poolOptions,
  poolState,
  volumeOptions,
  volumeState,
  isBlock,
  volumeNoticeId,
  allowHostPath,
  onUpdate,
  onRemove,
}: DiskRowProps) {
  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-center gap-2">
        <Select
          value={disk.kind}
          onValueChange={(v) => onUpdate({ kind: v as DiskDevice["kind"], source: "", path: "" })}
        >
          <SelectTrigger className="w-40" aria-label="Disk kind">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="volume">Custom volume</SelectItem>
            {instanceType === "container" && allowHostPath && (
              <SelectItem value="host">Host path</SelectItem>
            )}
          </SelectContent>
        </Select>
        <div className="flex-1" />
        <Button type="button" variant="ghost" size="icon" onClick={onRemove} aria-label="Remove disk">
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>

      {disk.kind === "volume" ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground" htmlFor={`create-disk-pool-${disk.id}`}>
              Pool
            </Label>
            <SearchableSelect
              id={`create-disk-pool-${disk.id}`}
              value={disk.pool}
              onValueChange={(v) => onUpdate({ pool: v, source: "" })}
              options={poolOptions}
              placeholder={poolState.disabled ? poolState.placeholder : "Select pool"}
              searchPlaceholder="Search pools…"
              emptyText={poolState.emptyText}
              disabled={poolState.disabled}
              ariaInvalid={poolState.notice?.kind === "error"}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground" htmlFor={`create-disk-volume-${disk.id}`}>
              Volume
            </Label>
            <SearchableSelect
              id={`create-disk-volume-${disk.id}`}
              value={disk.source}
              onValueChange={(v) => onUpdate({ source: v })}
              options={volumeOptions}
              placeholder={volumeState.placeholder}
              searchPlaceholder="Search volumes…"
              emptyText={volumeState.emptyText}
              disabled={volumeState.disabled}
              ariaDescribedBy={volumeState.notice ? volumeNoticeId : undefined}
              ariaInvalid={volumeState.notice?.kind === "error"}
            />
          </div>
        </div>
      ) : (
        <Input
          value={disk.source}
          onChange={(e) => onUpdate({ source: e.target.value })}
          placeholder="Host path (e.g. /srv/data)"
          aria-label="Host path"
        />
      )}

      {!(disk.kind === "volume" && isBlock) && (
        <Input
          value={disk.path}
          onChange={(e) => onUpdate({ path: e.target.value })}
          placeholder="Mount path (e.g. /data)"
          aria-label="Mount path"
        />
      )}
    </div>
  );
}
