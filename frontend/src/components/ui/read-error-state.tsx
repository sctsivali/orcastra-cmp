import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { describeReadError } from "@/lib/read-error";

// A list read that failed, told apart from a list that is empty.
//
// important: every list page used to gate on isLoading only, so a read that FAILED rendered
// the same "No X found" row a filter renders when it matches nothing, with the filter chip
// still on the toolbar. An operator following a link into a filtered list then read that as
// "the filter is wrong", cleared it, refreshed, and saw the rows, because the retry happened
// to succeed. The failure was real and the message blamed the filter. This says what
// happened and offers the retry, and says nothing about whether the rows exist.

interface ReadErrorStateProps {
  /** Plural noun, lowercase, ex; "instances". */
  noun: string;
  error: unknown;
  onRetry: () => void;
}

export function ReadErrorState({ noun, error, onRetry }: ReadErrorStateProps) {
  const { reason } = describeReadError(error);
  return (
    <EmptyState
      icon={AlertTriangle}
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">{title}</h1>
          {description && <p className="text-muted-foreground mt-2">{description}</p>}
        </div>
        {actions}
      </div>
      <Card>
        <CardContent className="pt-6">
          <ReadErrorState {...state} />
        </CardContent>
      </Card>
    </div>
  );
}
