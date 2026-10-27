"use client";

import * as React from "react";
import { Link2, Users } from "lucide-react";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import { Label } from "@/components/ui/label";
import type { OrgBranding } from "@/types/branding";

const INHERITED_NAME: Record<OrgBranding["inherits_from"], string> = {
  provider: "its provider",
  deployment: "the deployment default",
  default: "the built-in Orcastra look",
};

const NO_PROVIDER = "__none__";

/**
 * Where an organization's look comes from: its own row, its provider's (the partner that sold to
 * it, followed live), the deployment default, or the built-in look. An Administrator can choose
 * the provider; provisioning then leaves that choice alone.
 */
export function BrandingSourceNotice({
  orgName,
  meta,
  providerOptions,
  canChooseProvider,
  onProviderChange,
}: {
  orgName: string;
  meta: OrgBranding;
  providerOptions: { id: string; name: string }[];
  canChooseProvider: boolean;
  onProviderChange: (providerId: number | null) => void;
}) {
  const hasOwn = Object.keys(meta.settings).length > 0;
  const inherited = meta.inherits_from === "provider" && meta.provider ? meta.provider.name : INHERITED_NAME[meta.inherits_from];
  const options: SearchableSelectOption[] = React.useMemo(() => [
    { value: NO_PROVIDER, label: "No Provider", sublabel: "Use the deployment default" },
    ...providerOptions
      .filter((o) => o.id !== String(meta.organization_id))
      .map((o) => ({ value: o.id, label: o.name })),
  ], [providerOptions, meta.organization_id]);

  return (
    <div className="space-y-3 rounded-md border px-4 py-3 text-sm" data-testid="branding-source">
      <p className="flex items-start gap-2 text-muted-foreground">
        <Link2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>
          {hasOwn ? (
            <>
              <strong className="text-foreground">{orgName}</strong> uses its own branding. Reset it to show{" "}
              {inherited} again.
            </>
          ) : (
            <>
              <strong className="text-foreground">{orgName}</strong> shows {inherited}
              {meta.inherits_from === "provider" ? ", and follows its changes" : ""}. Changing anything below gives{" "}
              {orgName} branding of its own.
            </>
          )}
        </span>
      </p>
      {meta.followers_count > 0 && (
        <p className="flex items-start gap-2 text-muted-foreground">
          <Users className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>
            {meta.followers_count === 1 ? "1 organization takes" : `${meta.followers_count} organizations take`} their
            branding from this one, unless they set their own.
          </span>
        </p>
      )}
      {canChooseProvider && (
        <div className="space-y-1.5">
          <Label htmlFor="branding-provider">Branding Provider</Label>
          <SearchableSelect
            id="branding-provider"
            value={meta.provider ? String(meta.provider.id) : NO_PROVIDER}
            onValueChange={(value) => onProviderChange(value === NO_PROVIDER ? null : Number(value))}
            options={options}
            placeholder="No Provider"
            searchPlaceholder="Search organizations"
            className="w-full sm:w-[320px]"
          />
          <p className="text-xs text-muted-foreground">
            {meta.provider?.link_source === "provision"
              ? "Set when the organization bought from this partner."
              : meta.provider?.link_source === "migration"
                ? "Set from the sale records when this version was installed."
                : "Provisioning does not change a provider an Administrator chose."}
          </p>
        </div>
      )}
    </div>
  );
}
