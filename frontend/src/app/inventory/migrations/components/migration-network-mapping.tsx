"use client";

import { Network } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { SearchableSelect } from "@/components/ui/searchable-select";
import type { NetworkInfo } from "@/types/api";
import type { MigrationContractSummary, MigrationNicDecision } from "@/types/migrations";

const PARENT = "parent:";
const FOLLOW = "__follow__";

const VIA_LABEL: Record<MigrationNicDecision["via"], string> = {
  mapping: "Your mapping",
  target_profile: "Target profile",
  same_name: "Same network name",
  unmapped: "Needs a mapping",
  unattached: "Not attached",
};

function networkLabel(key: string | null): string {
  if (!key) return "None";
  return key.startsWith(PARENT) ? `${key.slice(PARENT.length)} (host interface)` : key;
}

/**
 * One row per NIC the plan resolved. Picking a network writes an explicit mapping keyed by the
 * source network, which the next preflight applies to every NIC on that network.
 */
export function MigrationNetworkMapping({
  contracts,
  mappings,
  networks,
  networksLoading,
  onChange,
}: {
  contracts: Array<{ name: string; contract: MigrationContractSummary }>;
  mappings: Record<string, string>;
  networks: NetworkInfo[];
  networksLoading: boolean;
  onChange: (mappings: Record<string, string>) => void;
}) {
  const rows = contracts.flatMap(({ name, contract }) =>
    (contract.nic_decisions ?? [])
      .filter((decision) => decision.via !== "unattached")
      .map((decision) => ({ instance: name, decision })),
  );
  const managed = networks.filter((network) => network.managed);
  const options = [
    { value: FOLLOW, label: "Decided by Preflight", sublabel: "Target profile or same name" },
    ...managed.map((network) => ({
      value: network.name,
      label: network.name,
      sublabel: `${network.type} · ${network.status}`,
    })),
    ...networks
      .filter((network) => !network.managed && network.type !== "loopback")
      .map((network) => ({
        value: `${PARENT}${network.name}`,
        label: network.name,
        sublabel: `Host ${network.type} interface`,
      })),
  ];

  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Network}
        title="No network interface to map"
        description="The source has no NIC, so nothing depends on a target network."
        className="py-8 rounded-md border"
      />
    );
  }

  return (
    <ul className="divide-y rounded-md border" aria-label="Network placement for each NIC">
      {rows.map(({ instance, decision }) => {
        const mapped = mappings[decision.source];
        const needsMapping = decision.via === "unmapped" && !mapped;
        const selectId = `network-map-${instance}-${decision.device}`.replace(/[^A-Za-z0-9_-]/g, "-");
        return (
          <li
            key={`${instance}:${decision.device}`}
            className="grid gap-2 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(14rem,1fr)] sm:items-center"
          >
            <div className="min-w-0 space-y-0.5">
              <p className="text-sm">
                <span className="font-mono">{decision.device}</span>
                <span className="text-muted-foreground"> on </span>
                <span className="font-mono">{instance}</span>
              </p>
              <p className="text-xs text-muted-foreground">
                Source network <span className="font-mono">{networkLabel(decision.source)}</span>
              </p>
              <p className={needsMapping ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {mapped
                  ? "Your mapping, applied on Continue"
                  : needsMapping
                    ? VIA_LABEL.unmapped
                    : `${VIA_LABEL[decision.via]}: ${networkLabel(decision.target)}`}
              </p>
            </div>
            <SearchableSelect
              id={selectId}
              value={mapped ?? FOLLOW}
              onValueChange={(value) => {
                const next = { ...mappings };
                if (value === FOLLOW) delete next[decision.source];
                else next[decision.source] = value;
                onChange(next);
              }}
              options={options}
              placeholder={networksLoading ? "Loading networks" : "Choose a network"}
              searchPlaceholder="Search networks"
              emptyText="No network reported by the target"
              disabled={networksLoading}
              ariaLabel={`Target network for ${decision.device} on ${instance}`}
              ariaInvalid={needsMapping}
            />
          </li>
        );
      })}
    </ul>
  );
}
