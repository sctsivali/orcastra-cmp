import type * as React from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import type { IllustrationProps } from "@/components/illustrations/illustration-frame";
import { DisconnectedIllustration } from "@/components/illustrations/scenes/states";
import { describeReadError } from "@/lib/read-error";

// A list read that failed, told apart from a list that is empty.
//
// important: a failed read must not render the "No X found" row a filter renders when it
// matches nothing. With a filter chip on the toolbar, an operator would blame the filter for a
// real failure. This says what happened, offers the retry, and says nothing about whether the
// rows exist.

interface ReadErrorStateProps {
  /** Plural noun, lowercase, ex; "instances". */
  noun: string;
  error: unknown;
  onRetry: () => void;
  /** Only where the failure is the whole page; inside a table or a panel the icon is enough. */
  illustration?: React.ComponentType<IllustrationProps>;
}

export function ReadErrorState({ noun, error, onRetry, illustration }: ReadErrorStateProps) {
  const { reason } = describeReadError(error);
  return (
    <EmptyState
      icon={AlertTriangle}
      illustration={illustration}
      title={`Could not load ${noun}`}
      description={`The ${noun} could not be read: ${reason}. This says nothing about whether they exist.`}
      action={
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    />
  );
}

interface PageReadErrorProps extends ReadErrorStateProps {
  /** The page heading, kept so the shell reads the same as the loaded page. */
  title: string;
  description?: string;
  /** The page's own header controls, ex; its RefreshButton. */
  actions?: React.ReactNode;
}

/** The page shell around ReadErrorState, for an early return right after the loading one. */
export function PageReadError({ title, description, actions, ...state }: PageReadErrorProps) {
  return (
    <div className="p-8 space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-bold">{title}</h1>
          {description && <p className="text-muted-foreground mt-2">{description}</p>}
        </div>
        {actions}
      </div>
      <Card>
        <CardContent className="pt-6">
          <ReadErrorState {...state} illustration={DisconnectedIllustration} />
        </CardContent>
      </Card>
    </div>
  );
}
