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
 * important: the query lives here rather than on the page. The page used to own it and pass
 * down only `data` and `isLoading`, so `error`, `isLoadingError`, `isRefetchError` and
 * `refreshNow` were all discarded before they could be rendered. Every failure - a cluster that
 * was down, one that was slow, a member that no longer existed, an account without access -
 * arrived as `resources === null` and painted the same "No resource data" block. Owning the
 * query is what makes dropping a field impossible rather than merely discouraged.
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
  // subject, so the hook's own double-click guard and pending hold are what keep the control
  // honest. It also toasts on failure, which the passive poll deliberately does not.
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
            populated panel because one poll tick did not land. The header's stale marker is
            what reports that instead. */}
        {resources ? (
          <MemberResourcePanels
            resources={resources}
            memberName={memberName}
            onShowInstances={handleShowInstances}
          />
        ) : isLoadingError ? (
          /* isLoadingError is isError && !hasData, which is exactly the case that has nothing
             to show. Using the library's own name keeps the invariant above unmissable. */
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
