"use client";

import * as React from "react";
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatLabel } from "@/lib/utils";
import type {
  MigrationCapability,
  MigrationMappingCategory,
  MigrationMappings,
} from "@/types/migrations";

const CATEGORIES: Exclude<MigrationMappingCategory, "storage_pools">[] = [
  "networks",
  "profiles",
  "volumes",
  "members",
  "external_dependencies",
];

const CAPABILITY_CODE: Record<(typeof CATEGORIES)[number], string> = {
  networks: "mapping_networks",
  profiles: "mapping_profiles",
  volumes: "mapping_volumes",
  members: "mapping_members",
  external_dependencies: "mapping_external_dependencies",
};

export function MigrationMappingsEditor({
  mappings,
  capabilities,
  onChange,
}: {
  mappings: MigrationMappings;
  capabilities: MigrationCapability[];
  onChange: (mappings: MigrationMappings) => void;
}) {
  const storageCapability = capabilities.find((item) => item.code === "mapping_storage_root");
  const storageSupported = storageCapability?.supported === true;
  const storageTarget = mappings.storage_pools.root ?? mappings.storage_pools["*"];
  const unsupportedStorageKeys = Object.keys(mappings.storage_pools).filter(
    (key) => key !== "root" && key !== "*",
  );

  return (
    <div className="space-y-5">
      <section className="space-y-3 rounded-md border p-3">
        <div>
          <h3 className="text-sm font-semibold">Storage Pools</h3>
          <p className="text-xs text-muted-foreground">
            {storageSupported
              ? "Only the instance root pool is executable. Change it in the Destination step."
              : storageCapability?.reason || "Root storage mapping was not advertised as executable."}
          </p>
        </div>
        {storageSupported ? (
          <div className="rounded-md border bg-muted/30 p-3">
            <p className="text-xs text-muted-foreground">Root storage target</p>
            <p className="mt-1 break-words font-mono text-sm font-medium">
              {storageTarget || "Project default pool"}
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            Unavailable for this plan. Select no storage override and resolve the dependency before execution.
          </p>
        )}
        {(unsupportedStorageKeys.length > 0 || (!storageSupported && Object.keys(mappings.storage_pools).length > 0)) && (
          <div className="space-y-2">
            <p className="text-sm text-destructive">
              {storageSupported
                ? `Unsupported storage keys: ${unsupportedStorageKeys.join(", ")}`
                : "The current storage mapping is unsupported."}
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onChange({
                ...mappings,
                storage_pools: storageSupported && storageTarget ? { root: storageTarget } : {},
              })}
            >
              Remove Unsupported Storage Mappings
            </Button>
          </div>
        )}
      </section>

      {CATEGORIES.map((category) => {
        const capability = capabilities.find((item) => item.code === CAPABILITY_CODE[category]);
        const values = mappings[category];
        return capability?.supported ? (
          <MappingCategory
            key={category}
            category={category}
            values={values}
            onChange={(next) => onChange({ ...mappings, [category]: next })}
          />
        ) : (
          <UnavailableMappingCategory
            key={category}
            category={category}
            values={values}
            reason={capability?.reason}
            onClear={() => onChange({ ...mappings, [category]: {} })}
          />
        );
      })}
    </div>
  );
}

function UnavailableMappingCategory({
  category,
  values,
  reason,
  onClear,
}: {
  category: Exclude<MigrationMappingCategory, "storage_pools">;
  values: Record<string, string>;
  reason?: string | null;
  onClear: () => void;
}) {
  const entries = Object.entries(values);
  return (
    <section className="space-y-3 rounded-md border p-3">
      <div>
        <h3 className="text-sm font-semibold">{formatLabel(category)}</h3>
        <p className="text-xs text-muted-foreground">
          {reason || "This mapping is not applied by the current migration executor."}
        </p>
      </div>
      {entries.length ? (
        <>
          <div className="divide-y rounded-md border bg-muted/30">
            {entries.map(([from, to]) => (
              <div key={from} className="grid gap-1 p-2 text-sm sm:grid-cols-2">
                <span className="break-words font-mono">{from}</span>
                <span className="break-words font-mono text-muted-foreground">{to}</span>
              </div>
            ))}
          </div>
          <Button type="button" variant="outline" size="sm" onClick={onClear}>
            Remove Unsupported Mappings
          </Button>
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          Unavailable for this plan. Resolve the dependency outside migration, then recheck.
        </p>
      )}
    </section>
  );
}

function MappingCategory({
  category,
  values,
  onChange,
}: {
  category: MigrationMappingCategory;
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
        <p className="text-xs text-muted-foreground">Map an exact source name to an exact destination name.</p>
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
          <Label htmlFor={sourceId}>Source name</Label>
          <Input id={sourceId} value={source} onChange={(event) => setSource(event.target.value)} autoComplete="off" />
        </div>
        <div className="space-y-2">
          <Label htmlFor={targetId}>Destination name</Label>
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
