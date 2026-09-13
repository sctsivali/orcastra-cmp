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

import {
  ACTIONS,
  PROTOCOLS,
  STATES,
  portsAllowed,
  rulePortErrors,
  type AclRule,
  type Direction,
} from "./acl-rules";

interface AclRuleRowProps {
  dir: Direction;
  index: number;
  rule: AclRule;
  onChange: (patch: Partial<AclRule>) => void;
  onRemove: () => void;
}

/** One ingress or egress rule: action, protocol, the far address, both ports, and its state. */
export function AclRuleRow({ dir, index, rule, onChange, onRemove }: AclRuleRowProps) {
  const ports = portsAllowed(rule);
  const errors = rulePortErrors(rule);
  const portId = (field: "source_port" | "destination_port") =>
    `acl-${dir}-${index}-${field === "source_port" ? "sport" : "dport"}`;
  const portField = (field: "source_port" | "destination_port", label: string, placeholder: string) => (
    <div className="space-y-1">
      <Label htmlFor={portId(field)} className="text-xs text-muted-foreground">
        {label}
      </Label>
      <Input
        id={portId(field)}
        value={rule[field] ?? ""}
        onChange={(e) => onChange({ [field]: e.target.value })}
        placeholder={ports ? placeholder : "Needs tcp or udp"}
        disabled={!ports}
        aria-invalid={errors[field] ? true : undefined}
        aria-describedby={errors[field] ? `${portId(field)}-error` : undefined}
      />
      {errors[field] && (
        <p id={`${portId(field)}-error`} className="text-xs text-destructive">
          {errors[field]}
        </p>
      )}
    </div>
  );
  return (
    <div className="grid grid-cols-2 sm:grid-cols-[1fr_1fr_1fr_1fr_1fr_auto] gap-2 rounded-lg border p-3 items-start">
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">Action</Label>
        <Select value={rule.action} onValueChange={(v) => onChange({ action: v })}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ACTIONS.map((a) => (
              <SelectItem key={a} value={a}>
                {a}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">Protocol</Label>
        <Select
          value={rule.protocol || "any"}
          onValueChange={(v) => {
            const protocol = v === "any" ? "" : v;
            // LXD refuses a port on anything but tcp and udp, so a port typed under tcp must
            // not travel along when the protocol changes to icmp or any.
            onChange(
              portsAllowed({ ...rule, protocol })
                ? { protocol }
                : { protocol, source_port: "", destination_port: "" },
            );
          }}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="any">any</SelectItem>
            {PROTOCOLS.filter(Boolean).map((p) => (
              <SelectItem key={p} value={p}>
                {p}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">
          {dir === "ingress" ? "Source" : "Destination"}
        </Label>
        <Input
          value={dir === "ingress" ? rule.source ?? "" : rule.destination ?? ""}
          onChange={(e) =>
            onChange(dir === "ingress" ? { source: e.target.value } : { destination: e.target.value })
          }
          placeholder="Any address"
        />
      </div>
      {portField("source_port", "Source port", "Any source port")}
      {portField("destination_port", "Destination port", "Any destination port")}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="mt-5"
        onClick={onRemove}
        aria-label={`Remove ${dir} rule ${index + 1}`}
      >
        <Trash2 className="h-4 w-4" />
      </Button>
      <div className="col-span-2 sm:col-span-6 space-y-1">
        <Label className="text-xs text-muted-foreground">State</Label>
        <Select value={rule.state || "enabled"} onValueChange={(v) => onChange({ state: v })}>
          <SelectTrigger className="sm:w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATES.map((st) => (
              <SelectItem key={st} value={st}>
                {st}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
