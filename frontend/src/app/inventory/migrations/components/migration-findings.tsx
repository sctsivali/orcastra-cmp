import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import type { MigrationIssue } from "@/types/migrations";
import {
  findingMessage,
  findingRemediation,
  findingTitle,
} from "./migration-format";

interface MigrationFindingsProps {
  blockers?: MigrationIssue[];
  warnings?: MigrationIssue[];
  showClear?: boolean;
}

export function MigrationFindings({ blockers = [], warnings = [], showClear = false }: MigrationFindingsProps) {
  if (blockers.length === 0 && warnings.length === 0 && showClear) {
    return (
      <EmptyState
        icon={CheckCircle2}
        title="Preflight Passed"
        description="No blocker or warning was reported. The plan is still rechecked before execution and cutover."
        className="py-8"
      />
    );
  }

  return (
    <div className="space-y-3">
      {blockers.map((finding, index) => {
        const remediation = findingRemediation(finding);
        return (
          <Alert key={`blocker-${index}`} variant="destructive">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertTitle>{findingTitle(finding) || "Migration blocked"}</AlertTitle>
            <AlertDescription>
              <p>{findingMessage(finding)}</p>
              {remediation && <p className="mt-1 font-medium">Resolution: {remediation}</p>}
            </AlertDescription>
          </Alert>
        );
      })}
      {warnings.map((finding, index) => {
        const remediation = findingRemediation(finding);
        return (
          <Alert key={`warning-${index}`}>
            <Info className="h-4 w-4" aria-hidden />
            <AlertTitle>{findingTitle(finding) || "Review before continuing"}</AlertTitle>
            <AlertDescription>
              <p>{findingMessage(finding)}</p>
              {remediation && <p className="mt-1 font-medium">Recommendation: {remediation}</p>}
            </AlertDescription>
          </Alert>
        );
      })}
    </div>
  );
}
