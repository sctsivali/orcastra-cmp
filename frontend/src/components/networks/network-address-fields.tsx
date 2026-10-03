"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { AddressField } from "./address-field";
import type { AddressOptions, BooleanOverride, NetworkOptions } from "./network-config";
import type { NetworkType } from "./network-rules";

export function BooleanOverrideField({ id, label, value, onChange, disabled }: {
  id: string; label: string; value: BooleanOverride; onChange: (next: BooleanOverride) => void; disabled?: boolean;
}) {
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>{label}</Label>
      <Select value={value} onValueChange={(next) => onChange(next as BooleanOverride)} disabled={disabled}>
        <SelectTrigger id={id}><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="default">Hypervisor default</SelectItem>
          <SelectItem value="true">Enabled</SelectItem>
          <SelectItem value="false">Disabled</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}

export function NetworkAddressFields({ type, options, onChange, errors, supportedKeys, disabled }: {
  type: NetworkType; options: NetworkOptions; onChange: (next: NetworkOptions) => void;
  errors: Record<string, string>; supportedKeys: string[]; disabled: boolean;
}) {
  const supports = (key: string) => supportedKeys.includes(key);
  const update = (family: "ipv4" | "ipv6", next: Partial<AddressOptions>) =>
    onChange({ ...options, [family]: { ...options[family], ...next } });
  return (
    <section className="space-y-5">
      <h3 className="text-sm font-semibold">Addresses</h3>
      {([4, 6] as const).map((version) => {
        const family = `ipv${version}` as const;
        const address = options[family];
        const enabled = address.mode !== "none";
        const canSetRanges = supports(`${family}.dhcp.ranges`) && address.mode === "custom" && address.dhcp !== "false" &&
          (version === 4 || (type === "bridge" && options.stateful === "true"));
        return (
          <div key={family} className="space-y-4">
            <AddressField label={`IPv${version} address`} which={`IPv${version}`} mode={address.mode}
              onModeChange={(mode) => update(family, { mode })} custom={address.custom}
              onCustomChange={(custom) => update(family, { custom })} error={errors[family] || ""} disabled={disabled} />
            {enabled && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {supports(`${family}.nat`) && <BooleanOverrideField id={`net-${family}-nat`} label={`IPv${version} NAT`}
                  value={address.nat} onChange={(nat) => update(family, { nat })} disabled={disabled} />}
                {supports(`${family}.dhcp`) && <BooleanOverrideField id={`net-${family}-dhcp`} label={`IPv${version} DHCP`}
                  value={address.dhcp} onChange={(dhcp) => update(family, { dhcp })} disabled={disabled} />}
                {version === 6 && address.dhcp !== "false" && supports("ipv6.dhcp.stateful") && (
                  <BooleanOverrideField id="net-ipv6-stateful" label="IPv6 stateful DHCP" value={options.stateful}
                    onChange={(stateful) => onChange({ ...options, stateful })} disabled={disabled} />
                )}
              </div>
            )}
            {canSetRanges && (
              <div className="grid gap-2">
                <Label htmlFor={`net-${family}-ranges`}>IPv{version} DHCP ranges</Label>
                <Input id={`net-${family}-ranges`} value={address.ranges} disabled={disabled} maxLength={4096}
                  onChange={(event) => update(family, { ranges: event.target.value })}
                  placeholder={version === 4 ? "10.10.10.10-10.10.10.200" : "fd42:1:2:3::10-fd42:1:2:3::100"}
                  aria-invalid={!!errors[`${family}.dhcp.ranges`]} aria-describedby={`net-${family}-ranges-hint`} />
                <p id={`net-${family}-ranges-hint`} className={`text-xs ${errors[`${family}.dhcp.ranges`] ? "text-destructive" : "text-muted-foreground"}`}>
                  {errors[`${family}.dhcp.ranges`] || "Optional. Separate FIRST-LAST ranges with commas. Leave blank for the hypervisor default."}
                </p>
              </div>
            )}
            {version === 6 && enabled && (
              <p className="text-xs text-muted-foreground">Use /64 for normal IPv6 router advertisements. DHCP ranges require Bridge stateful DHCP.</p>
            )}
          </div>
        );
      })}
    </section>
  );
}
