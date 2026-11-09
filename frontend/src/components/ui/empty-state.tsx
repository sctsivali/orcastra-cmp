import * as React from "react";
import { type LucideIcon } from "lucide-react";
import { IllustrationFallback, type IllustrationProps } from "@/components/illustrations/illustration-frame";
import { cn } from "@/lib/utils";

interface EmptyStateProps {
  /** Optional lucide icon shown above the title. */
  icon?: LucideIcon;
  /**
   * A scene for a first-run empty state ("No clusters yet"), never for a filter that matched
   * nothing. The icon stays as its fallback for when illustrations are hidden.
   */
  illustration?: React.ComponentType<IllustrationProps>;
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
export function EmptyState({ icon: Icon, illustration: Illustration, title, description, action, className, iconClassName }: EmptyStateProps) {
  const icon = Icon && <Icon className={cn("h-12 w-12 mx-auto text-muted-foreground mb-4", iconClassName)} aria-hidden="true" />;
  return (
    <div className={cn("text-center py-12", className)}>
      {Illustration ? (
        <>
          <Illustration size="md" className="mx-auto mb-4" />
          {icon && <IllustrationFallback>{icon}</IllustrationFallback>}
        </>
      ) : (
        icon
      )}
      <h3 className="text-lg font-semibold mb-2">{title}</h3>
      {description && (
        <p className="text-muted-foreground mb-4 max-w-md mx-auto">{description}</p>
      )}
      {action}
    </div>
  );
}
