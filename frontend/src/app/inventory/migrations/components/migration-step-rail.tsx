"use client";

import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { MigrationIntent } from "@/types/migrations";
import { MIGRATION_STEPS } from "./migration-wizard-model";

interface MigrationStepRailProps {
  step: number;
  intent: MigrationIntent;
  title: string;
  pending: boolean;
  onStepChange: (step: number) => void;
}

function displayLabel(label: string, index: number, intent: MigrationIntent): string {
  if (intent !== "copy") return label;
  if (index === 5) return "Copy Outcome";
  if (index === 8) return "Validation";
  if (index === 9) return "Completion";
  return label;
}

export function MigrationStepRail({
  step,
  intent,
  pending,
  onStepChange,
}: MigrationStepRailProps) {
  return (
    <nav className="hidden border-r bg-muted/20 p-4 md:block" aria-label="Migration steps">
      <ol className="space-y-1">
        {MIGRATION_STEPS.map((label, index) => {
          const reached = index <= step;
          const completed = index < step;
          return (
            <li key={label}>
              {index === 7 && (
                <p className="mb-1 mt-4 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Continues in job details
                </p>
              )}
              <button
                type="button"
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm",
                  index === step && "bg-background font-medium shadow-sm",
                  !reached && "text-muted-foreground",
                )}
                disabled={!reached || index > 6 || pending}
                onClick={() => onStepChange(index)}
                aria-current={index === step ? "step" : undefined}
              >
                <span
                  className={cn(
                    "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-xs",
                    completed && "border-status-online bg-status-online text-status-online-foreground",
                  )}
                >
                  {completed ? <Check className="h-3 w-3" aria-hidden /> : index + 1}
                </span>
                <span>{displayLabel(label, index, intent)}</span>
                {index > 6 && (
                  <span className="ml-auto rounded border px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide">
                    Job
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export function MigrationStepStatus({
  step,
  intent,
  title,
}: Pick<MigrationStepRailProps, "step" | "intent" | "title">) {
  return (
    <>
      <p className="text-sm text-muted-foreground md:hidden">
        Step {step + 1} of {MIGRATION_STEPS.length}: {title}
      </p>
      {step === 6 && (
        <p className="text-xs text-muted-foreground">
          {intent === "copy"
            ? "Execution, validation, and completion continue in the job detail after this plan is started."
            : "Execution, cutover, and recovery continue in the job detail after this plan is started."}
        </p>
      )}
    </>
  );
}
