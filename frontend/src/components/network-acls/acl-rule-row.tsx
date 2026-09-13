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

import { ACTIONS, PROTOCOLS, STATES, type AclRule, type Direction } from "./acl-rules";

interface AclRuleRowProps {
  dir: Direction;
  index: number;
  rule: AclRule;
  onChange: (patch: Partial<AclRule>) => void;
  onRemove: () => void;
}

/** One ingress or egress rule: action, protocol, the far address, the port, and its state. */
export function AclRuleRow({ dir, index, rule, onChange, onRemove }: AclRuleRowProps) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-[1fr_1fr_1fr_1fr_auto] gap-2 rounded-lg border p-3 items-end">
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
          onValueChange={(v) => onChange({ protocol: v === "any" ? "" : v })}
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
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">Destination port</Label>
        <Input
          value={rule.destination_port ?? ""}
          onChange={(e) => onChange({ destination_port: e.target.value })}
          placeholder="Any port"
        />
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={onRemove}
        aria-label={`Remove ${dir} rule ${index + 1}`}
      >
        <Trash2 className="h-4 w-4" />
      </Button>
      <div className="col-span-2 sm:col-span-5 space-y-1">
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
