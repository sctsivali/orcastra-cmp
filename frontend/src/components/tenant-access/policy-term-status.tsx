"use client";

import { Clock, Lock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { TableSubText } from "@/components/ui/table-text";
import type { AccessPolicy } from "@/types/api";
import { formatDate } from "@/lib/utils";

/**
 * Where a policy stands against the paid term OrcaHub sent for it (backend core/tenant_term.py).
 * Nothing renders for a policy with no term: most policies have none.
 */
export function PolicyTermStatus({ policy }: { policy: Pick<AccessPolicy, "phase" | "phase_inferred" | "term_ended_at" | "access_ends_at"> }) {
  const phase = policy.phase ?? "active";
  if (phase === "active") return null;
  if (phase === "ended") {
    return (
      <span className="mt-1 flex items-center gap-1.5">
        <Badge variant="negative">Ended</Badge>
        <TableSubText>{`Access ended ${formatDate(policy.access_ends_at)}`}</TableSubText>
      </span>
    );
  }
  const Icon = phase === "read_only" ? Lock : Clock;
  const text =
    phase === "ending"
      ? `Term ends ${formatDate(policy.term_ended_at ?? policy.access_ends_at)}`
      : policy.phase_inferred
        ? "Read only: narrowed by OrcaHub, no dates sent"
        : policy.access_ends_at
          ? `Read only until ${formatDate(policy.access_ends_at)}`
          : `Read only since ${formatDate(policy.term_ended_at)}`;
  return (
    <TableSubText className="mt-1 flex items-center gap-1" data-testid="policy-term-status" data-phase={phase}>
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {text}
    </TableSubText>
  );
}
