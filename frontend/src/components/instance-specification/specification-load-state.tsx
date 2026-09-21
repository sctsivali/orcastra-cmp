import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { DialogBody, DialogFooter } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { APIError } from "@/types/api";

interface SpecificationLoadStateProps {
  pending: boolean;
  error: unknown;
  onClose: () => void;
  onRetry: () => void;
}

export function SpecificationLoadState({
  pending,
  error,
  onClose,
  onRetry,
}: SpecificationLoadStateProps) {
  if (pending) {
    return (
      <>
        <DialogBody className="space-y-3" aria-busy="true" aria-label="Reading specifications">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-20 w-full" />
        </DialogBody>
        <DialogFooter className="shrink-0 pt-4">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
        </DialogFooter>
      </>
    );
  }

  const denied = error instanceof APIError && error.status === 403;
  return (
    <>
      <DialogBody>
        <Alert variant="destructive">
          <AlertTitle>{denied ? "Permission denied" : "Specifications unavailable"}</AlertTitle>
          <AlertDescription>
            {denied
              ? "You do not have permission to edit this instance."
              : error instanceof Error
                ? error.message
                : "The cluster did not answer."}
          </AlertDescription>
        </Alert>
      </DialogBody>
      <DialogFooter className="shrink-0 pt-4">
        <Button variant="outline" onClick={onClose}>Close</Button>
        {!denied && <Button onClick={onRetry}>Retry</Button>}
      </DialogFooter>
    </>
  );
}
