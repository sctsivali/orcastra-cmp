"use client";

import { Info } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * The created row exists but the active filters hide it.
 *
 * Informational, so it uses the partial-read notice's shell without the amber. Its one action
 * clears the filters, after which useRowFocus brings the row into view.
 */
export function RowFocusNotice({
  label,
  onShow,
  className,
  "data-testid": testId,
}: {
  label: string;
  onShow: () => void;
  className?: string;
  "data-testid"?: string;
}) {
  return (
    <div
      className={cn("flex items-start gap-3 rounded-md border bg-muted/50 p-3", className)}
      data-testid={testId ?? "row-focus-notice"}
      role="status"
    >
      <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <p className="text-sm text-muted-foreground">
        &quot;{label}&quot; was created but is hidden by the active filters.
        <button
          type="button"
          onClick={onShow}
          className="ml-1.5 underline decoration-dotted underline-offset-2 hover:decoration-solid"
        >
          Show it
        </button>
      </p>
    </div>
  );
}
