"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { DnsMode, NetworkOptions } from "./network-config";
import type { NetworkType } from "./network-rules";

export function NetworkServiceFields({ type, options, onChange, errors, disabled, aclNames, aclLoading, aclError, aclSupported, aclUnavailableReason, onRetryAcls }: {
  type: NetworkType; options: NetworkOptions; onChange: (next: NetworkOptions) => void;
  errors: Record<string, string>; disabled: boolean; aclNames: string[]; aclLoading: boolean;
  aclError: boolean; aclSupported: boolean; aclUnavailableReason: string; onRetryAcls: () => void;
}) {
  const [aclSearch, setAclSearch] = useState("");
  const filtered = aclNames.filter((name) => name.toLowerCase().includes(aclSearch.toLowerCase()));
  return (
    <>
      {type === "bridge" && (
        <section className="space-y-4">
          <h3 className="text-sm font-semibold">Bridge</h3>
          <div className="grid gap-2">
            <Label htmlFor="net-mtu">Bridge MTU</Label>
            <Input id="net-mtu" value={options.mtu} onChange={(event) => onChange({ ...options, mtu: event.target.value })}
              inputMode="numeric" placeholder="Hypervisor default" disabled={disabled} aria-invalid={!!errors.mtu} aria-describedby="net-mtu-hint" />
            <p id="net-mtu-hint" className={`text-xs ${errors.mtu ? "text-destructive" : "text-muted-foreground"}`}>
              {errors.mtu || "Optional. 68 to 65535 bytes; at least 1280 when IPv6 is enabled."}
            </p>
          </div>
        </section>
      )}
      <section className="space-y-4">
        <h3 className="text-sm font-semibold">DNS</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="grid gap-2">
            <Label htmlFor="net-dns-domain">DNS domain</Label>
            <Input id="net-dns-domain" value={options.dnsDomain} disabled={disabled} maxLength={254}
              onChange={(event) => onChange({ ...options, dnsDomain: event.target.value })} placeholder="Hypervisor default"
              aria-invalid={!!errors.dnsDomain} aria-describedby="net-dns-domain-hint" />
            <p id="net-dns-domain-hint" className={`text-xs ${errors.dnsDomain ? "text-destructive" : "text-muted-foreground"}`}>
              {errors.dnsDomain || "Optional domain advertised to DHCP clients and used for DNS resolution."}
            </p>
          </div>
          {type === "bridge" && (
            <div className="grid gap-2">
              <Label htmlFor="net-dns-mode">DNS mode</Label>
              <Select value={options.dnsMode} disabled={disabled} onValueChange={(value) => onChange({ ...options, dnsMode: value as DnsMode })}>
                <SelectTrigger id="net-dns-mode"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">Hypervisor default</SelectItem>
                  <SelectItem value="managed">Managed</SelectItem>
                  <SelectItem value="dynamic">Dynamic</SelectItem>
                  <SelectItem value="none">None</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      </section>
      <section className="space-y-3">
        <h3 className="text-sm font-semibold">Network ACLs</h3>
        <p className="text-xs text-muted-foreground">Optional ACLs from the network&apos;s project. Their unmatched-traffic policy also applies.</p>
        {!aclSupported ? (
          <p className="text-sm text-muted-foreground">{aclUnavailableReason}</p>
        ) : aclLoading ? (
          <p role="status" className="text-sm text-muted-foreground">Loading network ACLs…</p>
        ) : aclError ? (
          <div className="flex items-center gap-3 text-sm">
            <p role="alert" className="text-destructive">Network ACLs could not be loaded.</p>
            <Button variant="outline" size="sm" disabled={disabled} onClick={onRetryAcls}>Retry ACLs</Button>
          </div>
        ) : aclNames.length === 0 ? (
          <p className="text-sm text-muted-foreground">No network ACLs are available in this project.</p>
        ) : (
          <>
            {aclNames.length > 6 && (
              <div className="grid gap-2">
                <Label htmlFor="net-acl-search">Search network ACLs</Label>
                <Input id="net-acl-search" value={aclSearch} onChange={(event) => setAclSearch(event.target.value)} disabled={disabled} />
              </div>
            )}
            <div className="max-h-48 overflow-y-auto space-y-3 rounded-md border p-3">
              {filtered.length === 0 && <p className="text-sm text-muted-foreground">No ACLs match your search.</p>}
              {filtered.map((name) => (
                <div key={name} className="flex items-center gap-2">
                  <Checkbox id={`net-acl-${name}`} checked={options.acls.includes(name)} disabled={disabled || (options.acls.length >= 64 && !options.acls.includes(name))}
                    onCheckedChange={(checked) => onChange({ ...options, acls: checked === true
                      ? Array.from(new Set([...options.acls, name])) : options.acls.filter((selected) => selected !== name) })} />
                  <Label htmlFor={`net-acl-${name}`} className="font-normal break-all">{name}</Label>
                </div>
              ))}
            </div>
            {options.acls.length >= 64 && <p className="text-xs text-muted-foreground">At most 64 ACLs can be applied to a network.</p>}
          </>
        )}
      </section>
    </>
  );
}
