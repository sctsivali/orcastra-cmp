"use client";

import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useCertificateLifetimes } from "@/hooks/use-lxd";
import { formatDate } from "@/lib/utils";
import type { CertificateLifetime, CertificateLifetimeOption } from "@/types/api";

interface CertificateLifetimeSelectProps {
  id: string;
  /** What the operator picked; undefined until they pick, and then nothing is sent. */
  value: CertificateLifetime | undefined;
  onChange: (value: CertificateLifetime) => void;
  /**
   * What the server applies when nothing is sent, shown selected until the operator picks: the
   * deployment default at registration, the cluster's last lifetime on regenerate and renew.
   */
  fallback: CertificateLifetime;
  disabled?: boolean;
  /** Read the options only while the form is open: their dates count from the read. */
  enabled?: boolean;
}

function optionText(option: CertificateLifetimeOption): string {
  if (!option.available) return `${option.label} · not available`;
  return option.expires_at ? `${option.label} · until ${formatDate(option.expires_at)}` : option.label;
}

/** The lifetime of a cluster's client certificate: registration, regenerate and renew share it. */
export function CertificateLifetimeSelect({
  id, value, onChange, fallback, disabled, enabled = true,
}: CertificateLifetimeSelectProps) {
  const lifetimes = useCertificateLifetimes(enabled);
  const options = lifetimes.data?.options ?? [];
  // `||`, not `??`: inside a form Radix's hidden native select can report "", which is no choice.
  const shown = value || fallback;
  const selected = options.find((option) => option.value === shown);
  const reasons = Array.from(new Set(
    options.filter((option) => !option.available && option.reason).map((option) => option.reason as string),
  ));
  const helpId = `${id}-help`;

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>Certificate Validity</Label>
      {/* Always controlled: "" shows the placeholder, and a Select that starts uncontrolled keeps
          showing it after the options load. */}
      <Select
        value={selected ? shown : ""}
        onValueChange={(next) => {
          if (next) onChange(next as CertificateLifetime);
        }}
        disabled={disabled || lifetimes.isLoading || options.length === 0}
      >
        <SelectTrigger id={id} aria-describedby={helpId}>
          <SelectValue placeholder={lifetimes.isLoading ? "Loading lifetimes…" : "Default"} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value} disabled={!option.available}>
              {optionText(option)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <div id={helpId} className="space-y-1 text-xs text-muted-foreground">
        {lifetimes.isError ? (
          <p>The lifetimes could not be loaded, so nothing is chosen here and the usual lifetime applies.</p>
        ) : shown === "longest" ? (
          <p>
            The dashboard keeps access until the issuing certificate authority expires. Renew that
            authority before then.
          </p>
        ) : (
          <p>The dashboard loses access to the cluster when the certificate expires. Renew it before then.</p>
        )}
        {reasons.map((reason) => <p key={reason}>{reason}</p>)}
      </div>
    </div>
  );
}
