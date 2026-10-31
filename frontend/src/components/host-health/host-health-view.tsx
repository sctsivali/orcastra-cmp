"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { PageReadError } from "@/components/ui/read-error-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { TableLink } from "@/components/ui/table-link";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { useHostHealth } from "@/hooks/use-host-health";
import { formatClockTime } from "@/lib/utils";
import { HostCheckRow } from "./host-check-row";
import { HOST_STATUS } from "./status-display";
import { UnsealVaultDialog } from "./unseal-vault-dialog";

const TITLE = "Host Health";
const DESCRIPTION = "The services this dashboard itself runs on. Cluster warnings stay on the Warnings page.";

export function HostHealthView() {
  const { data, error, isLoading, isError, refetch, refreshNow, dataUpdatedAt } = useHostHealth({ poll: "page" });
  const [unsealOpen, setUnsealOpen] = useState(false);

  // The age shown is the report's own (when the backend checked), not when this browser fetched it.
  const refresh = (
    <RefreshButton
      label="Refresh host health"
      lastUpdatedAt={data ? data.collected_at * 1000 : dataUpdatedAt || null}
      onRefresh={refreshNow}
    />
  );

  if (isLoading) return <LoadingLottiePage />;
  if (!data) {
    return (
      <PageReadError
        title={TITLE}
        description={DESCRIPTION}
        noun="host health"
        error={error}
        onRetry={() => void refetch()}
        actions={refresh}
      />
    );
  }

  const considered = data.checks.filter((check) => check.status !== "not_configured").length;
  const overall = HOST_STATUS[data.overall];
  const collectedMs = data.collected_at * 1000;

  return (
    <div className="space-y-4 p-4 md:space-y-6 md:p-8">
      <div className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold md:text-3xl">{TITLE}</h1>
          <p className="mt-1 text-sm text-muted-foreground md:mt-2">
            The services this dashboard itself runs on.{" "}
            <TableLink href="/inventory/warnings">Cluster warnings</TableLink> stay on the Warnings page.
          </p>
        </div>
        {refresh}
      </div>

      <Card>
        <CardContent className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between md:p-5">
          <div className="flex flex-wrap items-center gap-2" role="status" aria-live="polite">
            <Badge variant={overall.tone}>{overall.label}</Badge>
            <p className="text-sm">
              {data.attention === 0
                ? `All ${considered} checked services are healthy.`
                : `${data.attention} of ${considered} checked services need attention.`}
            </p>
          </div>
          <p className="text-xs text-muted-foreground sm:text-right">
            Checked by the backend every {data.interval_seconds}s.
          </p>
        </CardContent>
      </Card>

      {isError && (
        <p className="text-sm text-muted-foreground" role="status">
          Could not refresh. Showing the report from {formatClockTime(collectedMs)}.
        </p>
      )}
      {data.store === "process" && (
        <p className="text-sm text-muted-foreground">
          Redis is not answering, so each backend worker reports on its own. Since-times can differ between refreshes.
        </p>
      )}

      <div className="space-y-3">
        {data.checks.map((check) => (
          <HostCheckRow key={check.id} check={check} onUnseal={() => setUnsealOpen(true)} />
        ))}
      </div>

      <UnsealVaultDialog open={unsealOpen} onOpenChange={setUnsealOpen} />
    </div>
  );
}
