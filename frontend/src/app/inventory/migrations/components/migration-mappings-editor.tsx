"use client";

import * as React from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatLabel } from "@/lib/utils";
import type { NetworkInfo } from "@/types/api";
import type {
  MigrationCapability,
  MigrationContractSummary,
  MigrationMappingCategory,
  MigrationMappings,
} from "@/types/migrations";
import { MigrationNetworkMapping } from "./migration-network-mapping";

const UNSUPPORTED: Array<Exclude<MigrationMappingCategory, "storage_pools" | "networks" | "profiles">> = [
  "volumes",
  "members",
  "external_dependencies",
];

export function MigrationMappingsEditor({
  mappings,
  capabilities,
  contracts,
  networks,
  networksLoading,
  onChange,
}: {
  mappings: MigrationMappings;
  capabilities: MigrationCapability[];
  contracts: Array<{ name: string; contract: MigrationContractSummary }>;
  networks: NetworkInfo[];
  networksLoading: boolean;
  onChange: (mappings: MigrationMappings) => void;
}) {
  const supported = (code: string) => capabilities.find((item) => item.code === code)?.supported === true;
  const rootPools = [...new Set(contracts.map(({ contract }) => contract.root_pool).filter(Boolean))];
  const pinned = contracts.flatMap(({ name, contract }) =>
    [...(contract.pinned_config ?? []), ...(contract.pinned_devices ?? [])].map((key) => `${name}: ${key}`),
  );

  return (
    <div className="space-y-5">
      <section className="space-y-2 rounded-md border p-3">
        <h3 className="text-sm font-semibold">Storage Pool</h3>
        <p className="text-xs text-muted-foreground">
          The root disk lands on this pool. Change it in the Destination step.
        </p>
        <p className="break-words font-mono text-sm font-medium">
          {rootPools.length ? rootPools.join(", ") : mappings.storage_pools.root || "Resolved by preflight"}
        </p>
      </section>

      <section className="space-y-3">
        <div>
          <h3 className="text-sm font-semibold">Networks</h3>
          <p className="text-xs text-muted-foreground">
            Each NIC keeps its network when the target has it, follows the target profile, or uses
            the network you pick here.
          </p>
        </div>
        {supported("mapping_networks") ? (
          <MigrationNetworkMapping
            contracts={contracts}
            mappings={mappings.networks}
            networks={networks}
            networksLoading={networksLoading}
            onChange={(networksMapping) => onChange({ ...mappings, networks: networksMapping })}
          />
        ) : (
          <p className="text-sm text-muted-foreground">Network mapping is not available for this plan.</p>
        )}
      </section>

      {supported("mapping_profiles") && (
        <MappingCategory
          category="profiles"
          description="Use an existing target profile in place of a source profile with another name."
          values={mappings.profiles}
          onChange={(profiles) => onChange({ ...mappings, profiles })}
        />
      )}

      {pinned.length > 0 && (
        <section className="space-y-2 rounded-md border p-3">
          <h3 className="text-sm font-semibold">Kept as on the Source</h3>
          <p className="text-xs text-muted-foreground">
            The target profiles would change these settings, so they are set on the instance itself.
          </p>
          <p className="break-words font-mono text-xs">{pinned.join(", ")}</p>
        </section>
      )}

      {UNSUPPORTED.filter((category) => Object.keys(mappings[category]).length > 0).map((category) => (
        <section key={category} className="space-y-2 rounded-md border p-3">
          <h3 className="text-sm font-semibold">{formatLabel(category)}</h3>
          <p className="text-sm text-destructive">
            {formatLabel(category)} mappings are not applied, so this plan stays blocked while they exist.
          </p>
          <Button type="button" variant="outline" size="sm" onClick={() => onChange({ ...mappings, [category]: {} })}>
            Remove {formatLabel(category)} Mappings
          </Button>
        </section>
      ))}
    </div>
  );
}

function MappingCategory({
  category,
  description,
  values,
  onChange,
}: {
  category: MigrationMappingCategory;
  description: string;
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
}) {
  const [source, setSource] = React.useState("");
  const [target, setTarget] = React.useState("");
  const entries = Object.entries(values);
  const sourceId = `mapping-${category}-source`;
  const targetId = `mapping-${category}-target`;
  const duplicate = !!source.trim() && source.trim() in values;
  const canAdd = !!source.trim() && !!target.trim() && !duplicate;

  const add = () => {
    if (!canAdd) return;
    onChange({ ...values, [source.trim()]: target.trim() });
    setSource("");
    setTarget("");
  };

  return (
    <section className="space-y-3 rounded-md border p-3">
      <div>
        <h3 className="text-sm font-semibold">{formatLabel(category)}</h3>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>

      {entries.length ? (
        <div className="divide-y rounded-md border">
          {entries.map(([from, to]) => (
            <div key={from} className="grid items-center gap-2 p-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto]">
              <span className="break-words font-mono text-sm">{from}</span>
              <Input
                aria-label={`Target for ${formatLabel(category)} ${from}`}
                value={to}
                onChange={(event) => onChange({ ...values, [from]: event.target.value })}
              />
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0"
                aria-label={`Remove mapping for ${from}`}
                onClick={() => {
                  const next = { ...values };
                  delete next[from];
                  onChange(next);
                }}
              >
                <Trash2 className="h-4 w-4" aria-hidden />
              </Button>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No explicit mapping.</p>
      )}

      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
        <div className="space-y-2">
          <Label htmlFor={sourceId}>Source Name</Label>
          <Input id={sourceId} value={source} onChange={(event) => setSource(event.target.value)} autoComplete="off" />
        </div>
        <div className="space-y-2">
          <Label htmlFor={targetId}>Destination Name</Label>
          <Input id={targetId} value={target} onChange={(event) => setTarget(event.target.value)} autoComplete="off" />
        </div>
        <Button type="button" variant="outline" onClick={add} disabled={!canAdd}>
          <Plus className="mr-2 h-4 w-4" aria-hidden />
          Add
        </Button>
      </div>
      {duplicate && <p className="text-xs text-destructive">That source already has a mapping.</p>}
    </section>
  );
}
