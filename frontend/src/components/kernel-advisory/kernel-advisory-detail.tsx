"use client";

import { CodeBlock } from "@/components/ui/code-block";
import { DetailField, DetailGrid, DetailSection } from "@/components/detail/detail-field";
import { Badge } from "@/components/ui/badge";
import { TableLink } from "@/components/ui/table-link";
import { UnreachableNodesBanner } from "@/components/ui/unreachable-nodes-banner";
import { formatDateTime } from "@/lib/utils";
import type { HostKernelRow, KernelAdvisoryInfo } from "@/types/kernel-advisory";
import { KERNEL_STATE_DISPLAY, osLabel, rowState } from "./kernel-advisory-display";

function scoreText(score: KernelAdvisoryInfo["scores"][number]): string {
  const parts = [score.score !== null ? score.score.toFixed(1) : null, score.rating].filter(Boolean);
  return `${score.source}: ${parts.join(" ")}`;
}

/** What one advisory is, where it is fixed and how current the data is. */
export function KernelAdvisorySummary({ advisory }: { advisory: KernelAdvisoryInfo }) {
  return (
    <DetailSection title={advisory.name ? `${advisory.id} (${advisory.name})` : advisory.id}>
      <p className="text-sm text-muted-foreground">{advisory.summary}</p>
      <DetailGrid cols={2}>
        <DetailField label="Severity" value={advisory.scores.map(scoreText).join(", ")} span="full" />
        <DetailField
          label="Fixed Upstream"
          value={`${advisory.fixed_upstream.join(", ")}, mainline ${advisory.fixed_mainline}`}
          span="full"
        />
        <DetailField label="Advisory Data As Of" value={advisory.data_as_of} />
        <DetailField
          label="Reference"
          value={
            <TableLink href={advisory.reference_url} external title="Open the advisory announcement">
              Kernel CVE announcement
            </TableLink>
          }
        />
      </DetailGrid>
    </DetailSection>
  );
}

/**
 * Every member's kernel and verdict, the commands that settle an unverified one, and what
 * could not be read.
 *
 * important: no mitigation is offered beyond a reboot into a fixed kernel. The vendors list no
 * workaround, and inventing one here would be worse than saying nothing.
 */
export function KernelAdvisoryMembers({
  rows,
  unreachable = [],
  reasons,
}: {
  rows: HostKernelRow[];
  unreachable?: string[];
  reasons?: Record<string, string>;
}) {
  const commands = Array.from(
    new Set(rows.flatMap((r) => r.findings.map((f) => f.check_command)).filter((c): c is string => !!c)),
  );
  const unlisted = rows.filter((r) => r.clustered && !r.members_listed);
  const collected = rows.map((r) => r.collected_at).filter((c): c is string => !!c).sort()[0];

  return (
    <div className="space-y-4">
      <UnreachableNodesBanner noun="host kernels" nodes={unreachable} reasons={reasons} />
      <DetailSection title="Cluster Members">
        {unlisted.length > 0 && (
          <p className="text-sm text-muted-foreground">
            The member list of {unlisted.map((r) => r.display_name).join(", ")} could not be read, so only the
            member answering the endpoint was checked.
          </p>
        )}
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No member of this cluster has been read yet.</p>
        ) : (
          <ul className="space-y-2">
            {rows.map((row) => {
              const state = KERNEL_STATE_DISPLAY[rowState(row)];
              return (
                <li key={`${row.cluster_id}/${row.member}`} className="rounded-lg border p-3 space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">{row.member}</span>
                    <Badge variant={state.tone}>{state.label}</Badge>
                  </div>
                  <DetailGrid cols={2}>
                    <DetailField label="Kernel" value={row.kernel_version ?? undefined} mono />
                    <DetailField label="OS" value={osLabel(row) ?? undefined} />
                  </DetailGrid>
                  {row.findings.map((finding) => (
                    <p key={finding.advisory_id} className="text-sm text-muted-foreground">
                      {finding.reason}
                    </p>
                  ))}
                </li>
              );
            })}
          </ul>
        )}
        {collected && (
          <p className="text-xs text-muted-foreground">Read from the cluster at {formatDateTime(collected)}.</p>
        )}
      </DetailSection>
      {commands.length > 0 && (
        <DetailSection
          title="Check On The Host"
          description="Run on the member to confirm. A fixed kernel only counts once the host has rebooted into it."
        >
          <div className="space-y-2">
            {commands.map((command) => (
              <CodeBlock key={command} value={command} copyLabel="Check command" />
            ))}
          </div>
        </DetailSection>
      )}
    </div>
  );
}
