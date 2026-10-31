"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { toast } from "@/hooks/use-toast";
import { useReleaseStatus } from "@/hooks/use-release-status";
import { reconcileMutationImpact } from "@/lib/mutation-coherence";
import { releaseAPI } from "@/lib/release-api";
import { formatDateTime, plural } from "@/lib/utils";
import { APIError } from "@/types/api";
import type { ReleaseLevel } from "@/types/release";
import { SOURCE_LABEL, releaseNoticeCopy, runningStatusText } from "./release-copy";

const TONE: Record<ReleaseLevel, BadgeTone> = { critical: "negative", warning: "caution", info: "neutral", none: "positive" };
const LEVEL_TEXT: Record<ReleaseLevel, string> = {
  critical: "Upgrade now",
  warning: "Upgrade soon",
  info: "Update available",
  none: "Up to date",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-3 py-1.5 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** Settings > Dashboard Host, Administrators: what the release manifest says, and "Check Now". */
export function ReleaseStatusCard() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useReleaseStatus();
  const check = useMutation({
    mutationFn: () => releaseAPI.checkNow(),
    onSuccess: async () => {
      await reconcileMutationImpact(queryClient, "release.check");
    },
    onError: (error) => {
      const wait = error instanceof APIError ? error.retryAfter : undefined;
      toast({
        title: "Could not check for releases",
        description: wait ? `The feed was read moments ago. Try again in ${plural(wait, "second")}.` : "Try again.",
        variant: "destructive",
      });
    },
  });
  const copy = data ? releaseNoticeCopy(data) : null;
  const placed = data && data.running_status !== null;

  return (
    <Card data-testid="release-status-card">
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:space-y-0">
        <div className="space-y-1.5">
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            Release
            {data && placed && <Badge variant={TONE[data.level]}>{LEVEL_TEXT[data.level]}</Badge>}
          </CardTitle>
          <CardDescription>
            Read twice a day from the public Orcastra release list. Nothing that identifies this deployment is sent.
          </CardDescription>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => check.mutate()}
          disabled={check.isPending || !data?.enabled}
          className="shrink-0"
        >
          {check.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <RefreshCw className="mr-2 h-4 w-4" aria-hidden="true" />
          )}
          Check Now
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading release status…</p>
        ) : isError || !data ? (
          <p className="text-sm text-muted-foreground">The release status could not be read.</p>
        ) : (
          <dl className="divide-y">
            {copy && <Row label="Notice">{copy.body}</Row>}
            <Row label="Running Version">{data.running_version ?? "Unknown"}</Row>
            <Row label="Status">{runningStatusText(data)}</Row>
            <Row label="Latest Release">{data.latest_version ?? "Not known yet"}</Row>
            <Row label="Last Checked">{data.checked_at ? formatDateTime(data.checked_at) : "Never"}</Row>
            {data.last_success_at && data.last_success_at !== data.checked_at && (
              <Row label="Last Read">{formatDateTime(data.last_success_at)}</Row>
            )}
            {data.source && <Row label="Source">{SOURCE_LABEL[data.source]}</Row>}
            {data.error && (
              <Row label="Last Error">
                <span className="font-mono text-xs break-all">{data.error}</span>
              </Row>
            )}
          </dl>
        )}
      </CardContent>
    </Card>
  );
}
