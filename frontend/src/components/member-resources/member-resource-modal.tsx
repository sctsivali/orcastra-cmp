"use client";

import * as React from "react";
import { LoadingLottieMedium } from "../loading-lottie";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { LiveTimestamp } from "@/components/ui/live-timestamp";
import { api } from "@/lib/api";
import { memberResourceFailure } from "@/lib/member-resource-error";
import { STALE_AFTER_MS } from "@/lib/query-config";
import { toMillis } from "@/lib/utils";
import { useManualRefresh } from "@/hooks/use-manual-refresh";
import { useMemberResources } from "@/hooks/use-lxd";
import type { VMInfo } from "@/types/api";
import { MemberInstancesDialog } from "./member-instances-dialog";
import { MemberResourcePanels } from "./member-resource-panels";

interface MemberResourceModalProps {
  isOpen: boolean;
  onClose: () => void;
  memberName: string | null;
  nodeName?: string | null;
}

/**
 * One cluster member's resources.
 *
 * important: the query lives here, not on the page, so `error`, `isLoadingError`,
 * `isRefetchError` and `refreshNow` reach the render. Passing only `data` down would turn
 * every failure (cluster down or slow, member gone, no access) into the same "No resource
 * data" block.
 */
export function MemberResourceModal({
  isOpen,
  onClose,
  memberName,
  nodeName,
}: MemberResourceModalProps) {
  const [showInstanceModal, setShowInstanceModal] = React.useState(false);
  const [instanceFilter, setInstanceFilter] = React.useState<"running" | "stopped" | null>(null);
  const [instances, setInstances] = React.useState<VMInfo[]>([]);
  const [loadingInstances, setLoadingInstances] = React.useState(false);
  const [instancesError, setInstancesError] = React.useState<string | null>(null);

  const {
    data: resources,
    error,
    isLoadingError,
    isRefetchError,
    refreshNow,
  } = useMemberResources(nodeName ?? "", memberName ?? "", { enabled: isOpen });

  // The forced refresh bypasses the 60s server cache and is rate limited to 12 a minute per
  // subject, hence the hook's double-click guard and pending hold. It toasts on failure; the
  // passive poll does not.
  const retry = useManualRefresh(refreshNow, {
    errorTitle: `Could not read resources for ${memberName ?? "this member"}`,
  });

  const failure = memberResourceFailure(error, {
    node: nodeName ?? "",
    member: memberName ?? "",
  });

  const handleShowInstances = async (filter: "running" | "stopped") => {
    if (!memberName || !nodeName) return;

    setInstanceFilter(filter);
    setLoadingInstances(true);
    setInstancesError(null);
    setShowInstanceModal(true);

    try {
      const response = await api.instances.list(nodeName);
      const wanted = filter === "running" ? "Running" : "Stopped";
      setInstances(
        (response.vms ?? []).filter(
          (vm) => vm.location === memberName && vm.status === wanted,
        ),
      );
    } catch (err) {
      console.error("Error loading instances:", err);
      setInstances([]);
      setInstancesError(err instanceof Error ? err.message : "Could not load instances");
    } finally {
      setLoadingInstances(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden">
        <DialogHeader className="pb-4">
          <DialogTitle>
            {memberName || "Member Resources"}
          </DialogTitle>
          <DialogDescription>
            Resource usage and availability for this cluster member
          </DialogDescription>
          {resources && (
            <LiveTimestamp
              className="mt-1"
              forceStale={isRefetchError}
              reason={
                isRefetchError
                  ? `${nodeName ?? "the cluster"} is not answering right now`
                  : undefined
              }
              staleAfterMs={STALE_AFTER_MS.attended}
              updatedAt={toMillis(resources.collected_at)}
            />
          )}
        </DialogHeader>

        <DialogBody className="pr-2">
        {/* important: data first, unconditionally. React Query keeps the last good body when a
            refetch fails (isRefetchError), so an error branch above this would blank a
            populated panel over one missed poll. The header's stale marker reports that. */}
        {resources ? (
          <MemberResourcePanels
            resources={resources}
            memberName={memberName}
            onShowInstances={handleShowInstances}
          />
        ) : isLoadingError ? (
          /* isLoadingError is isError && !hasData: the case with nothing to show. */
          <EmptyState
            action={
              failure.retryable ? (
                <Button
                  aria-busy={retry.pending}
                  disabled={retry.pending}
                  onClick={() => void retry.run()}
                  size="sm"
                  variant="outline"
                >
                  {retry.pending ? "Retrying" : "Retry"}
                </Button>
              ) : failure.gone ? (
                <Button onClick={onClose} size="sm" variant="outline">
                  Close
                </Button>
              ) : undefined
            }
            description={failure.description}
            icon={failure.icon}
            title={failure.title}
          />
        ) : (
          <div className="flex items-center justify-center py-12">
            <LoadingLottieMedium />
          </div>
        )}
        </DialogBody>
      </DialogContent>

      <MemberInstancesDialog
        error={instancesError}
        filter={instanceFilter}
        instances={instances}
        isLoading={loadingInstances}
        isOpen={showInstanceModal}
        memberName={memberName}
        onOpenChange={setShowInstanceModal}
        resources={resources ?? null}
      />
    </Dialog>
  );
}
