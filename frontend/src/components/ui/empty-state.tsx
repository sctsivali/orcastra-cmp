import * as React from "react";
import { type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  /** Optional lucide icon shown above the title. */
  icon?: LucideIcon;
  title: string;
  description?: string;
  /** Optional CTA (ex; a Create button) rendered below the description. */
  action?: React.ReactNode;
  className?: string;
  /** Extra classes on the icon (ex; "animate-spin" for a loading placeholder). */
  iconClassName?: string;
}

/**
 * Standard empty state for pages/tables with no data.
 * One shared shape across the app: centered icon + title + description + optional action.
 * For a table, wrap it in a single full-width cell:
 *   <TableRow><TableCell colSpan={N}><EmptyState .../></TableCell></TableRow>
 */
export function EmptyState({ icon: Icon, title, description, action, className, iconClassName }: EmptyStateProps) {
  return (
    <div className={cn("text-center py-12", className)}>
      {Icon && <Icon className={cn("h-12 w-12 mx-auto text-muted-foreground mb-4", iconClassName)} />}
      <h3 className="text-lg font-semibold mb-2">{title}</h3>
      {description && (
        <p className="text-muted-foreground mb-4 max-w-md mx-auto">{description}</p>
      )}
      {action}
    </div>
  );
}
