"use client";

import * as React from "react";
import { Activity } from "lucide-react";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { TableEmptyValue } from "@/components/ui/table-text";
import { UsageMeter } from "@/components/ui/usage-meter";
import { usagePercent } from "@/lib/usage-tone";
import { formatCpuTime, formatMemory } from "@/lib/utils";
import type { VMInfo } from "@/types/api";

const DESCRIPTION =
  "A single reading taken from the cluster when this dialog opened. The Metrics tab polls for live figures.";

/** A reading the cluster actually made. Zero is absent here: LXD reports 0 for what it cannot see. */
function reported(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

/**
 * What the cluster reports about one instance right now.
 *
 * important: the memory denominator is `memory_total`, not the peak-usage high-water mark this
 * card used to divide by. Every VM reports that peak as 0, so the old card printed "Memory
 * Limit 0" on every VM and the usage figure beside it had nothing to be a fraction of.
 *
 * important: an absent reading and a reading of nothing must not render the same. A running
 * instance whose guest says nothing comes back as usage 0 of total 0, and usagePercent turns
 * that into 0, so a bar drawn from it would be a confident green "idle" for an instance nobody
 * can see inside. That case gets an empty state, not a meter.
 */
export function InstanceResourcesSection({
  instance,
  isLoading,
  hasError,
}: {
  instance: VMInfo;
  /** The detail fetch that carries `resources`; the list row this dialog opens from never has it. */
  isLoading: boolean;
  hasError: boolean;
}) {
  const resources = instance.resources;

  // note: three different failures used to render as the section simply not being there, which
  // reads as a successful empty. A stopped instance, an unreachable node and a silent guest are
  // different problems and an operator has to be able to tell them apart.
  if (instance.status !== "Running") {
    return (
      <Section>
        <p className="text-sm text-muted-foreground">
          The cluster reports usage only while an instance is running.
        </p>
      </Section>
    );
  }

  if (!resources) {
    if (hasError) {
      return (
        <Section>
          <p className="text-sm text-muted-foreground">
            The reading could not be fetched. The node may be unreachable.
          </p>
        </Section>
      );
    }
    if (isLoading) {
      return (
        <Section>
          <div className="space-y-2" aria-hidden="true">
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-2.5 w-full" />
            <Skeleton className="h-4 w-40" />
          </div>
          <span className="sr-only">Loading resource readings</span>
        </Section>
      );
    }
    return (
      <Section>
        <p className="text-sm text-muted-foreground">
          The cluster did not return a reading for this instance.
        </p>
      </Section>
    );
  }

  const memoryUsed = resources.memory_usage;
  const memoryTotal = resources.memory_total;
  const cpuTime = resources.cpu_time_ns;

  // Everything failed together, so the section says so once instead of three placeholders.
  if (!reported(memoryUsed) && !reported(memoryTotal) && !reported(cpuTime)) {
    return (
      <Section>
        <EmptyState
          className="py-6"
          description="This instance is running, but the cluster reports no CPU or memory figures for it. Its guest agent is usually the reason."
          icon={Activity}
          title="No readings reported"
        />
      </Section>
    );
  }

  return (
    <Section>
      <div className="space-y-4">
        {reported(memoryTotal) && typeof memoryUsed === "number" ? (
          <UsageMeter
            label="Memory"
            total={memoryTotal}
            used={memoryUsed}
            value={`${formatMemory(memoryUsed)} / ${formatMemory(memoryTotal)} (${usagePercent(
              memoryUsed,
              memoryTotal,
            ).toFixed(1)}%)`}
          />
        ) : (
          <DetailGrid cols={2}>
            <DetailField
              label="Memory"
              value={<TableEmptyValue label="Memory not reported" />}
            />
          </DetailGrid>
        )}

        <DetailGrid cols={2}>
          {/* note: CPU time is cumulative and can exceed wall clock on a multi-core instance, so
              the qualifier is not decoration. Without it this reads as uptime. */}
          <DetailField
            label="CPU Time"
            value={
              typeof cpuTime === "number" ? (
                <span className="tabular-nums">
                  {formatCpuTime(cpuTime)}{" "}
                  {/* note: a real space, not just the margin. The gap is visual only, so the
                      accessible name and anything copied off the screen ran the value straight
                      into the qualifier as "15h 54mconsumed since start". */}
                  <span className="text-xs text-muted-foreground">consumed since start</span>
                </span>
              ) : (
                <TableEmptyValue label="CPU time not reported" />
              )
            }
          />
        </DetailGrid>
      </div>
    </Section>
  );
}

function Section({ children }: { children: React.ReactNode }) {
  return (
    <DetailSection description={DESCRIPTION} title="Resources">
      {children}
    </DetailSection>
  );
}
