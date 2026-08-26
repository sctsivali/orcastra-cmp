import type { ClusterAccessDisplay } from "@/lib/integration-display";
import { OverflowText } from "@/components/ui/overflow-text";

interface IntegrationClusterAccessListProps {
  clusters: ClusterAccessDisplay[];
}

export function IntegrationClusterAccessList({ clusters }: IntegrationClusterAccessListProps) {
  if (clusters.length === 0) {
    return (
      <div className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        No cluster access configured for this key.
      </div>
    );
  }

  return (
    <div className="rounded-md border bg-muted/30 p-3">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        Cluster access
      </p>
      <div className="space-y-2">
        {clusters.map((cluster, index) => (
          <div
            key={`${cluster.id}-${index}`}
            className="flex flex-col gap-2 rounded-md bg-background/70 px-3 py-2 sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="min-w-0">
              <p className="text-sm font-medium">
                <OverflowText value={cluster.name} label="cluster name" />
              </p>
              <p className="text-xs text-muted-foreground">
                <OverflowText
                  value={`${cluster.typeLabel}${cluster.name !== cluster.id ? ` - ${cluster.id}` : ""}`}
                  label={`id for ${cluster.name}`}
                />
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground sm:justify-end">
              <span className="font-medium text-foreground">{cluster.projectsLabel}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}