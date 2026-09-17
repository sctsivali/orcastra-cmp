"use client";

import { Plus, Trash2 } from "lucide-react";

import { DeviceIcon } from "@/components/detail/device-icon";
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

import { KeyValueRows } from "./key-value-rows";
import { DEVICE_TYPES, rowId, type DeviceDraft, type DeviceErrors } from "./profile-rules";

interface ProfileDevicesEditorProps {
  drafts: DeviceDraft[];
  onChange: (drafts: DeviceDraft[]) => void;
  errors: Record<string, DeviceErrors>;
  disabled?: boolean;
}

/** The devices of a profile, one bordered block each: name, type, and its properties. */
export function ProfileDevicesEditor({ drafts, onChange, errors, disabled }: ProfileDevicesEditorProps) {
  const patch = (id: string, next: Partial<DeviceDraft>) =>
    onChange(drafts.map((d) => (d.id === id ? { ...d, ...next } : d)));
  const remove = (id: string) => onChange(drafts.filter((d) => d.id !== id));
  const add = () => onChange([...drafts, { id: rowId(), name: "", type: "", props: [] }]);

  return (
    <div className="space-y-3">
      {drafts.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No devices. Instances that use this profile get their disks and interfaces elsewhere.
        </p>
      ) : (
        drafts.map((draft) => {
          const err = errors[draft.id];
          return (
            <div key={draft.id} className="space-y-3 rounded-lg border p-3">
              <div className="grid grid-cols-[auto_1fr_1fr_auto] gap-2 items-start">
                <DeviceIcon type={draft.type} className="mt-7" />
                <div className="space-y-1">
                  <Label htmlFor={`dev-${draft.id}-name`} className="text-xs text-muted-foreground">
                    Device name
                  </Label>
                  <Input
                    id={`dev-${draft.id}-name`}
                    className="font-mono"
                    value={draft.name}
                    onChange={(e) => patch(draft.id, { name: e.target.value })}
                    placeholder="eth0"
                    disabled={disabled}
                    aria-invalid={err?.name ? true : undefined}
                  />
                  {err?.name && <p className="text-xs text-destructive">{err.name}</p>}
                </div>
                <div className="space-y-1">
                  <Label htmlFor={`dev-${draft.id}-type`} className="text-xs text-muted-foreground">
                    Type
                  </Label>
                  <Select
                    value={draft.type}
                    onValueChange={(v) => patch(draft.id, { type: v })}
                    disabled={disabled}
                  >
                    <SelectTrigger id={`dev-${draft.id}-type`} aria-invalid={err?.type ? true : undefined}>
                      <SelectValue placeholder="Select a type" />
                    </SelectTrigger>
                    <SelectContent>
                      {DEVICE_TYPES.map((t) => (
                        <SelectItem key={t} value={t}>
                          {t}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {err?.type && <p className="text-xs text-destructive">{err.type}</p>}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="mt-5"
                  onClick={() => remove(draft.id)}
                  disabled={disabled}
                  aria-label={`Remove device ${draft.name || "without a name"}`}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
              <KeyValueRows
                rows={draft.props}
                onChange={(props) => patch(draft.id, { props })}
                errors={err?.props ?? {}}
                noun={`property of ${draft.name || "device"}`}
                emptyText="No properties. LXD applies the type's defaults."
                addLabel="Add property"
                keyPlaceholder="network"
                valuePlaceholder="lxdbr0"
                disabled={disabled}
                idPrefix={`dev-${draft.id}`}
              />
            </div>
          );
        })
      )}
      <Button type="button" variant="outline" size="sm" onClick={add} disabled={disabled}>
        <Plus className="h-4 w-4 mr-2" />
        Add device
      </Button>
    </div>
  );
}
