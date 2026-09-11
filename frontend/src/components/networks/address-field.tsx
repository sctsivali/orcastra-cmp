"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { IpMode } from "@/components/networks/network-rules";

interface AddressFieldProps {
  label: string;
  mode: IpMode;
  onModeChange: (mode: IpMode) => void;
  custom: string;
  onCustomChange: (value: string) => void;
  which: "IPv4" | "IPv6";
  error: string;
}

/** One address family of a network: auto, none, or a custom CIDR with its own reason line. */
export function AddressField({
  label,
  mode,
  onModeChange,
  custom,
  onCustomChange,
  which,
  error,
}: AddressFieldProps) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center">
        <Select value={mode} onValueChange={(v) => onModeChange(v as IpMode)}>
          <SelectTrigger className="sm:w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="auto">Auto</SelectItem>
            <SelectItem value="none">None</SelectItem>
            <SelectItem value="custom">Custom</SelectItem>
          </SelectContent>
        </Select>
        {mode === "custom" && (
          <Input
            value={custom}
            onChange={(e) => onCustomChange(e.target.value)}
            placeholder={which === "IPv4" ? "10.10.10.1/24" : "fd42:1:2:3::1/64"}
            className="flex-1"
            aria-invalid={!!error}
          />
        )}
      </div>
      {error ? (
        <p className="text-xs text-destructive">{error}</p>
      ) : (
        <p className="text-xs text-muted-foreground">
          Use CIDR notation. <span className="font-mono">auto</span> generates an unused subnet,{" "}
          <span className="font-mono">none</span> disables {which}.
        </p>
      )}
    </div>
  );
}
